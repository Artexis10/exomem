"""The sensor worker: gates, budgets, kill, refusal, and the real child process."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import sensing_fixture as sf

from exomem import (
    dreamer,
    freshness,
    runtime_resources,
    sensed_model,
    sensing_ledger,
    sensor_worker,
)


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


@pytest.mark.parametrize("reason", ["standby", "paused", "off"])
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


PEAK = 1362 << 20


def _record_peak(vault: Path, launcher: Launcher, sup, *, peak: int = PEAK) -> None:
    """The newest child ends having reported `peak`, and the supervisor harvests it."""
    child = launcher.children[-1]
    child.write({"cpu": 5.0, "judgements": 4, "final": True, "peak_rss_bytes": peak})
    child.exit(sensor_worker.EXIT_IDLE)
    sup._harvest()


@pytest.mark.parametrize("available", [PEAK, PEAK - 1], ids=["equal", "below"])
def test_a_launch_needs_available_memory_above_the_recorded_peak(
    tmp_path: Path, monkeypatch, available
) -> None:
    sf.enable(monkeypatch)
    vault = tmp_path / "vault"
    sup, launcher = _supervisor(vault, [1000.0])
    assert _step(sup) == "launched"
    _record_peak(vault, launcher, sup)
    monkeypatch.setattr(runtime_resources, "available_memory", lambda: available)
    assert _step(sup) == "memory"
    assert len(launcher.children) == 1
    monkeypatch.setattr(runtime_resources, "available_memory", lambda: PEAK + 1)
    assert _step(sup) == "launched"


def test_a_peak_recorded_for_another_instrument_does_not_block_a_launch(
    tmp_path: Path, monkeypatch
) -> None:
    sf.enable(monkeypatch)
    vault = tmp_path / "vault"
    sup, launcher = _supervisor(vault, [1000.0])
    assert _step(sup) == "launched"
    _record_peak(vault, launcher, sup)
    monkeypatch.setattr(runtime_resources, "available_memory", lambda: PEAK - 1)
    assert _step(sup) == "memory"
    sf.enable(monkeypatch, sf.with_identity(revision="9"))
    assert _step(sup) == "launched", "the old peak says nothing about the new model"
    assert sup.peak_rss_bytes() is None


def test_a_host_that_cannot_read_free_memory_still_launches(tmp_path: Path, monkeypatch) -> None:
    sf.enable(monkeypatch)
    vault = tmp_path / "vault"
    sup, launcher = _supervisor(vault, [1000.0])
    assert _step(sup) == "launched"
    _record_peak(vault, launcher, sup)
    monkeypatch.setattr(runtime_resources, "available_memory", lambda: None)
    assert _step(sup) == "launched"
    assert len(launcher.children) == 2


@pytest.mark.parametrize("available", [0, None], ids=["none_free", "unknown"])
def test_with_no_recorded_peak_the_first_child_launches_and_measures_it(
    tmp_path: Path, monkeypatch, available
) -> None:
    sf.enable(monkeypatch)
    monkeypatch.setattr(runtime_resources, "available_memory", lambda: available)
    sup, launcher = _supervisor(tmp_path / "vault", [1000.0])
    assert sup.peak_rss_bytes() is None
    assert _step(sup) == "launched"
    assert len(launcher.children) == 1


def test_a_running_child_is_never_killed_for_memory(tmp_path: Path, monkeypatch) -> None:
    sf.enable(monkeypatch)
    vault = tmp_path / "vault"
    sup, launcher = _supervisor(vault, [1000.0])
    sensor_worker._write_json(
        sensing_ledger.ledger_path(vault).with_name(sensor_worker.PEAK_FILE),
        {"peak_rss_bytes": PEAK, "key": sensor_worker._refusal_key()},
    )
    monkeypatch.setattr(runtime_resources, "available_memory", lambda: PEAK * 2)
    assert _step(sup) == "launched"
    monkeypatch.setattr(runtime_resources, "available_memory", lambda: 0)
    assert _step(sup) == "running"
    assert not launcher.children[0].terminated


def test_the_recorded_peak_survives_a_restart_and_a_cut_off_child_cannot_lower_it(
    tmp_path: Path, monkeypatch
) -> None:
    sf.enable(monkeypatch)
    monkeypatch.setattr(runtime_resources, "available_memory", lambda: 1 << 40)
    vault = tmp_path / "vault"
    sup, launcher = _supervisor(vault, [1000.0])
    _step(sup)
    _record_peak(vault, launcher, sup)
    assert sup.status()["peak_rss_bytes"] == PEAK
    restarted, relauncher = _supervisor(vault, [2000.0])
    assert restarted.peak_rss_bytes() == PEAK, "the next service process reads it back"

    # A child killed mid model load reports a small, non-final peak: that is a
    # lower bound, so it must not lower what a whole child needed.
    _step(restarted)
    relauncher.children[-1].write({"cpu": 1.0, "judgements": 0, "final": False, "peak_rss_bytes": 80 << 20})
    assert _step(restarted, run=False, reason="standby") == "gate:standby"
    assert restarted.peak_rss_bytes() == PEAK

    # A refused child never loaded the instrument either.
    _step(restarted)
    refused = relauncher.children[-1]
    refused.write({"cpu": 0.5, "judgements": 0, "final": True, "peak_rss_bytes": 90 << 20})
    refused.exit(sensor_worker.EXIT_REFUSED)
    _step(restarted)
    assert restarted.peak_rss_bytes() == PEAK

    # A whole child's final report replaces it, so a smaller model can lower it.
    sf.enable(monkeypatch, sf.with_identity(revision="9"))
    _step(restarted)
    _record_peak(vault, relauncher, restarted, peak=PEAK // 2)
    assert restarted.peak_rss_bytes() == PEAK // 2


def test_the_child_reports_its_peak_memory_where_the_host_gives_one(
    tmp_path: Path, monkeypatch
) -> None:
    sf.enable(monkeypatch)
    vault = sf.build(tmp_path)
    sf.settle(vault)
    spend_path = sensing_ledger.ledger_path(vault).with_name(sensor_worker.SPEND_FILE)

    monkeypatch.setattr(runtime_resources, "peak_rss_bytes", lambda: PEAK)
    assert sf.sense(vault, sf.StubInstrument(sf.default_table())) == sensor_worker.EXIT_IDLE
    assert json.loads(spend_path.read_text())["peak_rss_bytes"] == PEAK

    monkeypatch.setattr(runtime_resources, "peak_rss_bytes", lambda: None)
    assert sf.sense(vault, sf.StubInstrument(sf.default_table())) == sensor_worker.EXIT_IDLE
    assert "peak_rss_bytes" not in json.loads(spend_path.read_text())


def test_question_admission_refusal_keeps_the_recorded_model_peak(
    tmp_path: Path, monkeypatch
) -> None:
    from exomem import sensing_nli

    sf.enable(monkeypatch)
    vault = sf.build(tmp_path)
    sf.settle(vault)
    monkeypatch.setattr(runtime_resources, "available_memory", lambda: 1 << 40)
    sup, launcher = _supervisor(vault, [1000.0])
    _step(sup)
    _record_peak(vault, launcher, sup)
    _step(sup)
    child = launcher.children[-1]

    def missing_weights():
        raise sensing_nli.Refused("weights-missing", "the artifact is unavailable")

    with monkeypatch.context() as child_host:
        child_host.setattr(sensor_worker.os, "getpid", lambda: child.pid)
        child_host.setattr(runtime_resources, "peak_rss_bytes", lambda: 80 << 20)
        code = sensor_worker.run_child(
            vault, parent_pid=0, cpu_allotment=10, judgement_allotment=10,
            idle_seconds=0, admit=missing_weights,
        )
    assert code == sensor_worker.EXIT_IDLE, "question refusal does not stop other questions"
    child.exit(code)
    sup._harvest()
    assert sup.peak_rss_bytes() == PEAK
    assert sup.status()["refused"] is None, "the supervisor has no global admission refusal"


def test_question_refusal_backoff_survives_parent_child_setting_difference(
    tmp_path: Path, monkeypatch
) -> None:
    from exomem import sensing, sensing_nli

    sf.enable(monkeypatch)
    vault = sf.build(tmp_path)
    sf.settle(vault)

    def missing_weights():
        raise sensing_nli.Refused("weights-missing", "the artifact is unavailable")

    # The sanitized child does not inherit the supervisor's setting override.
    with monkeypatch.context() as child_environment:
        child_environment.setenv("EXOMEM_SENSING", "off")
        assert sensor_worker.run_child(
            vault, parent_pid=0, cpu_allotment=10, judgement_allotment=10,
            idle_seconds=0, admit=missing_weights,
        ) == sensor_worker.EXIT_IDLE
    assert sensing.enabled()
    assert sensor_worker.question_refused(vault, sensing.PAIR_RELATION)
    sup, launcher = _supervisor(vault, [1000.0])
    monkeypatch.setattr(sensor_worker, "supervisor", lambda _vault: sup)
    assert sensor_worker.supervise(vault, gate_run=True, gate_reason="ready") == "idle"
    assert launcher.children == [], "known refused work must not relaunch a child"


def test_a_cloud_cell_never_launches_the_child(tmp_path: Path, monkeypatch) -> None:
    """Until the Cloud placement exists, cells write no readings."""
    sf.enable(monkeypatch)
    sup, launcher = _supervisor(tmp_path / "vault", [1000.0])
    assert _step(sup) == "launched"
    monkeypatch.setenv("EXOMEM_CLOUD_CELL", "1")
    assert _step(sup) == "cloud_placement"
    assert launcher.children[0].terminated
    assert _step(sup) == "cloud_placement"
    assert len(launcher.children) == 1, "no relaunch while the cell stays a Cloud cell"


def test_turning_sensing_off_mid_run_keeps_the_recorded_peak(tmp_path: Path, monkeypatch) -> None:
    sf.enable(monkeypatch)
    vault = tmp_path / "vault"
    sup, launcher = _supervisor(vault, [1000.0])
    assert _step(sup) == "launched"
    _record_peak(vault, launcher, sup)
    monkeypatch.setattr(runtime_resources, "available_memory", lambda: PEAK + 1)
    assert _step(sup) == "launched"
    launcher.children[-1].write({"cpu": 0.1, "judgements": 0, "peak_rss_bytes": 80 << 20})
    monkeypatch.setenv("EXOMEM_SENSING", "off")
    assert _step(sup) == "off"
    sf.enable(monkeypatch)
    monkeypatch.setattr(runtime_resources, "available_memory", lambda: PEAK - 1)
    assert _step(sup) == "memory", "a cut-off report under the setting change must not erase the peak"


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


def test_quiet_mode_leaves_the_sensor_child_running(tmp_path: Path, monkeypatch) -> None:
    """Quiet turns off the GPU and model preload, not the CPU-only child."""
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
    assert seen == [(True, "ready")]


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


def test_a_real_child_with_no_jobs_exits_without_admitting_or_creating_a_ledger(
    tmp_path: Path, monkeypatch
) -> None:
    """Lazy admission must not touch missing weights when the queue is empty."""
    sf.enable(monkeypatch)
    vault = tmp_path / "vault"
    vault.mkdir()
    monkeypatch.setenv("HF_HUB_CACHE", str(tmp_path / "empty-hub"))
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    monkeypatch.setattr(sensor_worker, "IDLE_EXIT_SECONDS", 0.0)
    child = sensor_worker._launch_child(vault, 10.0, 5)
    assert child.wait(timeout=30) == sensor_worker.EXIT_IDLE
    assert not sensing_ledger.ledger_path(vault).exists()
    status = sensing_ledger.ledger_path(vault).with_name(sensor_worker.STATUS_FILE)
    assert not status.exists(), "no instrument was admitted or refused"


def test_stored_unit_vectors_never_create_the_embeddings_sidecar(tmp_path: Path) -> None:
    from exomem import index_paths

    vault = tmp_path / "vault"
    vault.mkdir()
    assert sensed_model.encoder_fingerprint(vault) is None
    assert sensed_model.stored_unit_vectors(vault, "Knowledge Base/x.md", "any|cls|l2") == {}
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
    assert calls[:3] == [(True, "ready")] * 3
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


def test_completed_and_refused_prefix_cannot_starve_another_question(
    tmp_path: Path, monkeypatch
) -> None:
    from dataclasses import replace

    from exomem import sensing

    sf.enable(monkeypatch)
    vault = tmp_path / "vault"
    store = sensed_model.ProjectionStore(vault)
    conn = store.connect()
    with store.write(conn):
        sensed_model._set_meta(conn, "seeding", None)
    conn.close()
    future = replace(
        sensing.question(sensing.PAIR_RELATION),
        question_type="test.future",
        vector_names=("answer",),
    )
    monkeypatch.setitem(sensing.QUESTIONS, future.question_type, future)
    base_active = sensed_model.active_instruments
    monkeypatch.setattr(
        sensed_model,
        "active_instruments",
        lambda q=sensing.PAIR_RELATION: (
            base_active() if q == sensing.PAIR_RELATION else {sf.STUB.instrument_id: sf.STUB}
        ),
    )
    texts = tuple(sorted(("A completed unit.", "Another completed unit."), key=sensing.text_sha256))
    units = tuple(
        sensing.InputUnit(str(i), f"{i}.md", sensing.text_sha256(text))
        for i, text in enumerate(texts)
    )
    complete = sensing.SenseRequest(sensing.PAIR_RELATION, 1, units, texts)
    ledger = sensing_ledger.Ledger(vault)
    lconn = ledger.connect()
    reading = sensing.make_request_reading(
        complete,
        sf.STUB,
        vectors={"ab": sf.NEUTRAL, "ba": sf.NEUTRAL},
        abstain_reason=None,
        sensed_at="2026-01-01T00:00:00Z",
    )
    ledger.append(lconn, [reading])
    lconn.close()
    future_inputs = tuple(
        sensing.InputSlot(str(i), f"{i}.md", slot, "page-claim-v1", sensing.text_sha256(text))
        for i, (slot, text) in enumerate(zip(("a", "b"), texts, strict=True))
    )
    pending_future = sensing.SenseRequest(future.question_type, 2, future_inputs, texts)
    pending = sensing.SenseRequest(sensing.PAIR_RELATION, 2, future_inputs, (texts))
    jobs = [
        sensed_model.OwnedRequest(f"complete-{i:04d}", complete, order_key=f"a-{i:04d}")
        for i in range(300)
    ]
    jobs += [
        sensed_model.OwnedRequest(f"refused-{i:04d}", pending_future, order_key=f"b-{i:04d}")
        for i in range(300)
    ]
    jobs += [sensed_model.OwnedRequest("runnable", pending, order_key="z")]
    token = sensed_model.capture_scope(vault, "audit", "scope")
    sensed_model.submit_scope(vault, token, jobs)
    admitted = []
    stub = sf.StubInstrument()

    def admit(q):
        admitted.append(q)
        if q == future.question_type:

            class Refused(Exception):
                reason = "weights-missing"
                detail = "test artifact unavailable"

            raise Refused()
        return stub

    assert (
        sensor_worker.run_child(
            vault,
            parent_pid=0,
            cpu_allotment=100,
            judgement_allotment=10,
            idle_seconds=0,
            admit_selected=admit,
            sleep=lambda _: None,
        )
        == sensor_worker.EXIT_IDLE
    )
    assert len(stub.calls) == 1
    assert admitted.count(future.question_type) == 1
    assert sensor_worker.question_refused(vault, future.question_type)
    assert (
        list(
            sensed_model.runnable_jobs(
                vault, refused=lambda q: sensor_worker.question_refused(vault, q)
            )
        )
        == []
    )


def test_restored_artifacts_clear_only_the_refused_question(tmp_path: Path, monkeypatch) -> None:
    from exomem import claims, model_cache, sensing

    sf.enable(monkeypatch)
    vault = tmp_path / "vault"
    monkeypatch.setenv("HF_HUB_CACHE", str(tmp_path / "hub"))
    pin = claims._active_pin()
    assert pin is not None
    before = sensor_worker._question_refusal_key(vault, sensing.PAIR_RELATION)
    sensor_worker._write_json(
        sensor_worker._state_file(vault, sensor_worker.STATUS_FILE),
        {
            "refusals": {sensing.PAIR_RELATION: {"key": before, "reason": "weights-missing"}},
        },
    )
    assert sensor_worker.question_refused(vault, sensing.PAIR_RELATION)
    snapshot = (
        model_cache.hub_dir()
        / model_cache.snapshot_dirname(pin.model_name)
        / "snapshots"
        / pin.model_revision
    )
    for relative in pin.artifact_files:
        artifact = snapshot / relative
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_bytes(b"invented admission evidence; never loaded")
    assert not sensor_worker.question_refused(vault, sensing.PAIR_RELATION)
