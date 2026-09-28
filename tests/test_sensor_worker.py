"""The sensor worker: gates, budgets, kill, refusal, and the real child process."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
import sensing_fixture as sf

from exomem import dreamer, freshness, sensed_model, sensing_ledger, sensor_worker


@pytest.fixture(autouse=True)
def _clean():
    freshness.clear()
    dreamer.reset_for_tests()
    sensor_worker.reset_for_tests()
    yield
    dreamer.reset_for_tests()
    sensor_worker.reset_for_tests()
    freshness.clear()


class FakeChild:
    _next_pid = 40000

    def __init__(self, vault: Path, *, report: dict | None = None) -> None:
        FakeChild._next_pid += 1
        self.pid = FakeChild._next_pid
        self.returncode: int | None = None
        self.terminated = False
        self.killed = False
        self.vault = vault
        if report is not None:
            self.write(report)

    def write(self, report: dict) -> None:
        path = sensing_ledger.ledger_path(self.vault).with_name(sensor_worker.SPEND_FILE)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"pid": self.pid, **report}), encoding="utf-8")

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    def kill(self):
        self.killed = True
        self.returncode = -9

    def wait(self, timeout=None):
        return self.returncode

    def exit(self, code: int) -> None:
        self.returncode = code


class Launcher:
    def __init__(self, vault: Path) -> None:
        self.vault = vault
        self.children: list[FakeChild] = []
        self.allotments: list[tuple[float, int]] = []

    def __call__(self, vault_root, cpu, judgements):
        self.allotments.append((cpu, judgements))
        child = FakeChild(self.vault, report={"cpu": 0.0, "judgements": 0, "final": False})
        self.children.append(child)
        return child


def _supervisor(vault: Path, now: list[float]) -> tuple[sensor_worker.Supervisor, Launcher]:
    launcher = Launcher(vault)
    return sensor_worker.Supervisor(vault, launch=launcher, clock=lambda: now[0]), launcher


def _step(sup, *, run=True, reason="ready", depth=3):
    return sup.step(gate_run=run, gate_reason=reason, queue_depth=lambda: depth)


def test_launches_only_with_every_gate_open(tmp_path: Path, monkeypatch) -> None:
    now = [1000.0]
    sup, launcher = _supervisor(tmp_path / "vault", now)
    assert _step(sup) == "off", "sensing is off by default"
    sf.enable(monkeypatch)
    assert _step(sup, run=False, reason="foreground") == "gate:foreground"
    assert _step(sup, depth=0) == "idle"
    assert launcher.children == []
    assert _step(sup) == "launched"
    assert _step(sup) == "running"
    assert len(launcher.children) == 1


@pytest.mark.parametrize("reason", ["quiet_mode", "pressure", "standby", "paused", "foreground"])
def test_a_closed_gate_kills_the_child(tmp_path: Path, monkeypatch, reason: str) -> None:
    sf.enable(monkeypatch)
    now = [1000.0]
    sup, launcher = _supervisor(tmp_path / "vault", now)
    assert _step(sup) == "launched"
    assert _step(sup, run=False, reason=reason) == f"gate:{reason}"
    assert launcher.children[0].terminated
    assert not sup.alive()
    assert _step(sup, run=False, reason=reason) == f"gate:{reason}"
    assert len(launcher.children) == 1, "no relaunch while the gate stays closed"


def test_turning_sensing_off_kills_the_child(tmp_path: Path, monkeypatch) -> None:
    sf.enable(monkeypatch)
    sup, launcher = _supervisor(tmp_path / "vault", [1000.0])
    _step(sup)
    monkeypatch.setenv("EXOMEM_SENSING", "off")
    assert _step(sup) == "off"
    assert launcher.children[0].terminated


def test_the_hourly_budget_holds_across_launches(tmp_path: Path, monkeypatch) -> None:
    sf.enable(monkeypatch)
    now = [1000.0]
    sup, launcher = _supervisor(tmp_path / "vault", now)
    _step(sup)
    first = launcher.children[0]
    first.write({"cpu": 200.0, "judgements": 100, "final": True})
    first.exit(sensor_worker.EXIT_IDLE)
    assert _step(sup) == "launched"
    assert launcher.allotments[-1] == (100.0, 500)
    second = launcher.children[1]
    second.write({"cpu": 100.0, "judgements": 10, "final": True})
    second.exit(sensor_worker.EXIT_BUDGET)
    assert _step(sup) == "budget"
    now[0] += sensor_worker.WINDOW_SECONDS
    assert _step(sup) == "launched"


def test_an_unreported_death_is_charged_its_whole_allotment(tmp_path: Path, monkeypatch) -> None:
    sf.enable(monkeypatch)
    now = [1000.0]
    sup, launcher = _supervisor(tmp_path / "vault", now)
    _step(sup)
    child = launcher.children[0]
    sensing_ledger.ledger_path(tmp_path / "vault").with_name(sensor_worker.SPEND_FILE).unlink()
    child.exit(-9)
    assert _step(sup) == "budget"
    assert sup.used() == (sensor_worker.CPU_SECONDS_PER_HOUR, sensor_worker.JUDGEMENTS_PER_HOUR)


def test_a_refusal_suspends_relaunch_until_the_instrument_changes(
    tmp_path: Path, monkeypatch
) -> None:
    sf.enable(monkeypatch)
    vault = tmp_path / "vault"
    sup, launcher = _supervisor(vault, [1000.0])
    _step(sup)
    child = launcher.children[0]
    status = sensing_ledger.ledger_path(vault).with_name(sensor_worker.STATUS_FILE)
    status.write_text(json.dumps({"refused": "weights-missing", "pid": child.pid}), encoding="utf-8")
    child.write({"cpu": 0.5, "judgements": 0, "final": True})
    child.exit(sensor_worker.EXIT_REFUSED)
    assert _step(sup) == "refused"
    assert _step(sup) == "refused"
    assert sup.status()["refused"] == "weights-missing"
    assert len(launcher.children) == 1
    sf.enable(monkeypatch, sf.with_identity(revision="9"))
    assert _step(sup) == "launched"


def test_the_dreamer_loop_supervises_and_polls_fast_while_a_child_lives(
    tmp_path: Path, monkeypatch
) -> None:
    sf.enable(monkeypatch)
    vault = tmp_path / "vault"
    seen: list[tuple[bool, str]] = []
    monkeypatch.setattr(
        sensor_worker,
        "supervise",
        lambda root, *, gate_run, gate_reason: seen.append((gate_run, gate_reason)) or "ok",
    )
    monkeypatch.setenv("EXOMEM_MODE", "quiet")
    monkeypatch.setenv("EXOMEM_DREAMER", "on")
    dreamer._loop_once(vault, dreamer.Clock())
    assert seen == [(False, "quiet_mode")]


def test_the_child_senses_only_what_the_ledger_lacks(tmp_path: Path, monkeypatch) -> None:
    sf.enable(monkeypatch)
    vault = sf.build(tmp_path)
    sf.settle(vault)
    stub = sf.StubInstrument(sf.default_table())
    assert sf.sense(vault, stub) == sensor_worker.EXIT_IDLE
    calls = len(stub.calls)
    assert calls > 0
    # Without an ingesting tick the queue still names the pairs: the child
    # skips every pair whose reading already exists.
    assert sf.sense(vault, stub) == sensor_worker.EXIT_IDLE
    assert len(stub.calls) == calls


def test_the_child_stops_at_its_judgement_allotment(tmp_path: Path, monkeypatch) -> None:
    sf.enable(monkeypatch)
    vault = sf.build(tmp_path)
    sf.settle(vault)
    stub = sf.StubInstrument(sf.default_table())
    code = sensor_worker.run_child(
        vault, parent_pid=0, cpu_allotment=1e9, judgement_allotment=1, idle_seconds=0.0,
        instrument=stub, sleep=lambda _s: None,
    )
    assert code == sensor_worker.EXIT_BUDGET
    assert len(stub.calls) == 1
    spend = json.loads(
        sensing_ledger.ledger_path(vault).with_name(sensor_worker.SPEND_FILE).read_text()
    )
    assert spend["final"] is True and spend["judgements"] == 1


def test_an_orphaned_child_exits(tmp_path: Path, monkeypatch) -> None:
    sf.enable(monkeypatch)
    vault = sf.build(tmp_path)
    sf.settle(vault)
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    stub = sf.StubInstrument(sf.default_table())
    code = sensor_worker.run_child(
        vault, parent_pid=dead.pid, cpu_allotment=1e9, judgement_allotment=100,
        idle_seconds=30.0, instrument=stub, sleep=lambda _s: None,
    )
    assert code == sensor_worker.EXIT_IDLE
    assert stub.calls == []


def test_a_real_child_without_weights_refuses_and_is_not_relaunched(
    tmp_path: Path, monkeypatch
) -> None:
    """The real spawn path: a child process that cannot admit its instrument."""
    sf.enable(monkeypatch)
    vault = tmp_path / "vault"
    vault.mkdir()
    monkeypatch.setenv("HF_HUB_CACHE", str(tmp_path / "empty-hub"))
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    sup = sensor_worker.Supervisor(vault)
    assert _step(sup) == "launched"
    deadline = time.monotonic() + 60
    while sup.alive() and time.monotonic() < deadline:
        time.sleep(0.1)
    assert not sup.alive()
    assert _step(sup) == "refused"
    assert sup.status()["refused"] in {"dependency-missing", "weights-missing"}
    assert sup.launches == 1
    assert not sensing_ledger.ledger_path(vault).exists(), "a refused child appends nothing"


def test_stored_unit_vectors_never_create_the_embeddings_sidecar(tmp_path: Path) -> None:
    from exomem import index_paths

    vault = tmp_path / "vault"
    vault.mkdir()
    assert sensed_model.stored_unit_vectors(vault, "Knowledge Base/x.md") == (None, {})
    assert not index_paths.sidecar_path(vault).exists()
    assert os.environ.get("EXOMEM_SENSING") in {None, "", "off"}


def test_a_living_child_is_re_gated_between_ticks_without_a_tick(
    tmp_path: Path, monkeypatch
) -> None:
    import threading

    vault = tmp_path / "vault"
    calls: list[tuple[bool, str]] = []
    ticks: list[int] = []
    stop = threading.Event()
    monkeypatch.setattr(sensor_worker, "POLL_WHILE_ALIVE_SECONDS", 0.01)
    monkeypatch.setattr(sensor_worker, "alive", lambda root: True)
    monkeypatch.setattr(dreamer, "run_once", lambda *a, **k: ticks.append(1))

    def supervise(root, *, gate_run, gate_reason):
        calls.append((gate_run, gate_reason))
        if len(calls) >= 3:
            stop.set()
        return "gate"

    monkeypatch.setattr(sensor_worker, "supervise", supervise)
    monkeypatch.setenv("EXOMEM_MODE", "quiet")
    monkeypatch.setenv("EXOMEM_DREAMER", "on")
    assert dreamer._wait(vault, dreamer.Clock(), stop, 30.0) is True
    assert calls[:3] == [(False, "quiet_mode")] * 3
    assert ticks == []


def test_unusable_output_refuses_that_batch_and_the_child_goes_on(
    tmp_path: Path, monkeypatch
) -> None:
    sf.enable(monkeypatch)
    vault = sf.build(tmp_path)
    sf.settle(vault)

    class Broken(sf.StubInstrument):
        def judge(self, pairs):
            self.calls.extend(pairs)
            raise ValueError("non-finite logits")

    broken = Broken()
    assert sf.sense(vault, broken) == sensor_worker.EXIT_IDLE
    assert broken.calls, "the child tried"
    assert not sensing_ledger.ledger_path(vault).exists() or sensing_ledger.max_seq(
        sensing_ledger.open_readonly(vault)
    ) == 0


def test_the_child_gets_an_allowlisted_environment(tmp_path: Path, monkeypatch) -> None:
    captured: dict = {}

    class Popen:
        def __init__(self, args, env=None, **kwargs):
            captured["args"], captured["env"] = args, env

    monkeypatch.setattr(sensor_worker.subprocess, "Popen", Popen)
    for name in (
        "EXOMEM_INTERNAL_INGRESS_KEY",
        "GITHUB_TOKEN",
        "AWS_SECRET_ACCESS_KEY",
        "OPENAI_API_KEY",
        "HF_TOKEN",
        "EXOMEM_OAUTH_CLIENT_SECRET",
        "SOME_PASSWORD",
        "UNRELATED_SETTING",
    ):
        monkeypatch.setenv(name, "must-not-leak")
    monkeypatch.setenv("HF_HUB_CACHE", str(tmp_path / "hub"))
    monkeypatch.setenv("LC_ALL", "C.UTF-8")
    sensor_worker._launch_child(tmp_path / "vault", 10.0, 5)
    env = captured["env"]
    assert "must-not-leak" not in env.values()
    assert not [key for key in env if any(word in key for word in ("SECRET", "PASSWORD"))]
    assert env["HF_HUB_OFFLINE"] == "1" and env["TRANSFORMERS_OFFLINE"] == "1"
    assert env["CUDA_VISIBLE_DEVICES"] == "" and env["OMP_NUM_THREADS"] == "1"
    assert env["HF_HUB_CACHE"] == str(tmp_path / "hub")
    assert env["LC_ALL"] == "C.UTF-8"
    assert env["EXOMEM_STATE_ROOT"] == os.environ["EXOMEM_STATE_ROOT"]
    assert env.get("PATH") == os.environ.get("PATH")
