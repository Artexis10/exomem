"""D1-T1: the dreamer's idle gate and budgets, as a pure decision.

`dreamer_policy.decide` sees only a `TickSignals` snapshot and a clock value.
Every gate is a signal the worker probes once per loop. A tick starts when no
request is live and the graph owes nothing; the idle and settle preferences hold
it for at most `MAX_DEFER_SECONDS` since the last tick. `sensor_gate` is the
sensor child's own, narrower gate.
"""

from __future__ import annotations

import dataclasses

import pytest

from exomem import dreamer_policy as policy


def _ready(**overrides) -> policy.TickSignals:
    base = policy.TickSignals(
        setting="on",
        standby=False,
        pressure=False,
        idle_seconds=3600.0,
        vault_quiet_seconds=3600.0,
        last_whole_vault_pass=None,
        graph_debt=False,
        full_upsert_backlog=0,
        freshness_live=True,
        hour_cpu_used=0.0,
        consecutive_failures=0,
        seconds_since_failure=None,
        # A tick completed just now, so no bound has waived an idle or settle hold.
        seconds_since_tick=0.0,
    )
    return dataclasses.replace(base, **overrides)


def test_default_is_off_and_starts_nothing() -> None:
    assert policy.resolve_setting(None, {}) == "off"
    assert policy.resolve_setting("", {"mode": "quiet"}) == "off"
    assert policy.resolve_setting(None, {"dreamer": "bogus"}) == "off"
    decision = policy.decide(_ready(setting="off"))
    assert decision.run is False
    assert decision.reason == "off"
    assert decision.page_budget == 0


def test_env_overrides_config_and_paused_keeps_state() -> None:
    assert policy.resolve_setting(None, {"dreamer": "on"}) == "on"
    assert policy.resolve_setting("off", {"dreamer": "on"}) == "off"
    assert policy.resolve_setting("paused", {"dreamer": "on"}) == "paused"
    assert policy.resolve_setting("ON", {}) == "on"
    decision = policy.decide(_ready(setting="paused"))
    assert decision.run is False
    assert decision.reason == "paused"
    # Paused is a live thread that keeps its checkpoint: it keeps polling.
    assert decision.sleep_s == policy.POLL_SECONDS


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"standby": True}, "standby"),
        ({"pressure": True}, "pressure"),
        ({"idle_seconds": 0.0}, "foreground"),
        ({"idle_seconds": policy.IDLE_SECONDS - 1}, "foreground"),
        ({"vault_quiet_seconds": policy.SETTLE_FLOOR_SECONDS - 1}, "settling"),
        (
            {"vault_quiet_seconds": 200.0, "last_whole_vault_pass": 300.0},
            "settling",
        ),
        ({"graph_debt": True}, "graph_debt"),
        ({"full_upsert_backlog": 1}, "index_backlog"),
        ({"freshness_live": False}, "freshness_unavailable"),
        ({"hour_cpu_used": policy.HOURLY_CPU_SECONDS}, "budget_exhausted"),
        ({"consecutive_failures": 3, "seconds_since_failure": 1.0}, "backoff"),
    ],
)
def test_each_gate_signal_blocks_a_tick(overrides, reason) -> None:
    assert policy.decide(_ready()).run is True
    decision = policy.decide(_ready(**overrides))
    assert decision.run is False
    assert decision.reason == reason
    assert decision.page_budget == 0
    assert decision.sleep_s > 0


def test_settle_tracks_the_last_whole_vault_pass() -> None:
    assert policy.settle_seconds(None) == policy.SETTLE_FLOOR_SECONDS
    assert policy.settle_seconds(5.0) == policy.SETTLE_FLOOR_SECONDS
    assert policy.settle_seconds(240.0) == 240.0
    # Quiet for longer than the floor but shorter than one whole-vault pass:
    # graph convergence still owns that window.
    held = policy.decide(_ready(vault_quiet_seconds=120.0, last_whole_vault_pass=240.0))
    assert (held.run, held.reason) == (False, "settling")
    released = policy.decide(_ready(vault_quiet_seconds=241.0, last_whole_vault_pass=240.0))
    assert released.run is True


def test_a_write_burst_holds_every_tick() -> None:
    # A write every second for ten minutes, a tick just completed: the vault
    # never goes quiet, so no decision may run, however idle the foreground is.
    last_write = 0.0
    for now in range(0, 600):
        last_write = float(now)
        decision = policy.decide(
            _ready(vault_quiet_seconds=float(now) - last_write, idle_seconds=3600.0)
        )
        assert decision.run is False
        assert decision.reason == "settling"
    # After the burst the gate opens only once the settle window has passed.
    assert policy.decide(_ready(vault_quiet_seconds=59.0)).run is False
    assert policy.decide(_ready(vault_quiet_seconds=60.0)).run is True


@pytest.mark.parametrize(
    ("busy", "reason"),
    [
        ({"idle_seconds": 30.0}, "foreground"),
        ({"vault_quiet_seconds": 30.0}, "settling"),
        ({"idle_seconds": 30.0, "vault_quiet_seconds": 30.0}, "foreground"),
    ],
    ids=["requests", "writes", "both"],
)
def test_a_busy_vault_holds_a_recent_tick_and_runs_one_past_the_bound(busy, reason) -> None:
    held = policy.decide(_ready(**busy, seconds_since_tick=10.0))
    assert (held.run, held.reason) == (False, reason)
    # The hold ends when the preference does or the bound passes, whichever is first.
    assert held.sleep_s <= policy.MAX_DEFER_SECONDS - 10.0
    released = policy.decide(_ready(**busy, seconds_since_tick=policy.MAX_DEFER_SECONDS))
    assert released.run is True
    assert released.page_budget == policy.PAGES_PER_TICK


def test_a_vault_that_is_never_idle_still_gets_a_tick_every_bound() -> None:
    # A day of continuous use, polled as the worker polls: a request ended 5 s
    # ago and a write 5 s ago at every poll. Every hold names the signal, and a
    # tick starts within the bound plus one poll of the last one.
    reasons: set[str] = set()
    ticks = 0
    since_tick = 0.0
    for _poll in range(int(24 * 3600 / policy.POLL_SECONDS)):
        decision = policy.decide(
            _ready(idle_seconds=5.0, vault_quiet_seconds=5.0, seconds_since_tick=since_tick)
        )
        if decision.run:
            ticks += 1
            since_tick = 0.0
        else:
            reasons.add(decision.reason)
        since_tick += policy.POLL_SECONDS
    assert reasons == {"foreground"}
    assert ticks >= 24 * 3600 // (policy.MAX_DEFER_SECONDS + policy.POLL_SECONDS)


def test_a_live_request_holds_a_tick_even_past_the_bound() -> None:
    decision = policy.decide(
        _ready(idle_seconds=0.0, seconds_since_tick=10 * policy.MAX_DEFER_SECONDS)
    )
    assert (decision.run, decision.reason) == (False, "foreground")


def test_graph_debt_and_the_other_hard_gates_hold_past_the_bound() -> None:
    past = {"seconds_since_tick": 10 * policy.MAX_DEFER_SECONDS, "idle_seconds": 5.0}
    for overrides, reason in (
        ({"graph_debt": True}, "graph_debt"),
        ({"full_upsert_backlog": 1}, "index_backlog"),
        ({"freshness_live": False}, "freshness_unavailable"),
        ({"hour_cpu_used": policy.HOURLY_CPU_SECONDS}, "budget_exhausted"),
        ({"consecutive_failures": 3, "seconds_since_failure": 1.0}, "backoff"),
    ):
        decision = policy.decide(_ready(**past, **overrides))
        assert (decision.run, decision.reason) == (False, reason)


@pytest.mark.parametrize(
    "overrides",
    [
        {"idle_seconds": 0.0},
        {"vault_quiet_seconds": 0.0},
        {"graph_debt": True},
        {"full_upsert_backlog": 5},
        {"freshness_live": False},
        {"hour_cpu_used": policy.HOURLY_CPU_SECONDS},
        {"consecutive_failures": 3, "seconds_since_failure": 1.0},
        {"pressure": True},
    ],
    ids=[
        "live_request",
        "settling",
        "graph_debt",
        "index_backlog",
        "freshness_unavailable",
        "tick_cpu_budget",
        "backoff",
        "gpu_pressure",
    ],
)
def test_the_sensor_child_runs_whatever_holds_a_tick(overrides) -> None:
    # The child is its own process with its own budgets, and CPU only: nothing
    # the tick waits for stops it, and GPU pressure does not apply to it.
    assert policy.sensor_gate(_ready(**overrides)).run is True


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"setting": "off"}, "off"),
        ({"setting": "paused"}, "paused"),
        ({"standby": True}, "standby"),
    ],
)
def test_the_sensor_child_waits_for_the_operators_posture(overrides, reason) -> None:
    assert policy.sensor_gate(_ready()).run is True
    decision = policy.sensor_gate(_ready(**overrides))
    assert (decision.run, decision.reason) == (False, reason)


def test_duty_cycle_sleeps_at_least_four_ticks() -> None:
    assert policy.sleep_after_tick(0.0) == policy.MIN_SLEEP_SECONDS
    assert policy.sleep_after_tick(0.1) == policy.MIN_SLEEP_SECONDS
    assert policy.sleep_after_tick(1.0) == pytest.approx(4.0)
    assert policy.sleep_after_tick(3.0) >= 4 * 3.0
    decision = policy.decide(_ready())
    assert decision.run is True
    assert decision.page_budget == policy.PAGES_PER_TICK
    # The in-tick budget stops a tick on pages, thread CPU or wall time.
    assert policy.tick_exhausted(pages=policy.PAGES_PER_TICK, cpu=0.0, wall=0.0) == "pages"
    assert policy.tick_exhausted(pages=1, cpu=policy.TICK_CPU_SECONDS, wall=0.0) == "cpu"
    assert policy.tick_exhausted(pages=1, cpu=0.0, wall=policy.TICK_WALL_SECONDS) == "wall"
    assert policy.tick_exhausted(pages=1, cpu=0.01, wall=0.01) is None


def test_failure_backoff_doubles_and_caps() -> None:
    assert policy.backoff_seconds(2) == 0.0
    assert policy.backoff_seconds(3) == 60.0
    assert policy.backoff_seconds(4) == 120.0
    assert policy.backoff_seconds(40) == policy.BACKOFF_CAP_SECONDS
    ready = policy.decide(_ready(consecutive_failures=3, seconds_since_failure=61.0))
    assert ready.run is True
