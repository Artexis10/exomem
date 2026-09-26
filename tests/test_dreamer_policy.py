"""D1-T1: the dreamer's idle gate and budgets, as a pure decision.

`dreamer_policy.decide` sees only a `TickSignals` snapshot and a clock value.
Every gate is a signal the worker probes once per loop; a tick runs only when
all of them say the service is idle and the graph owes nothing.
"""

from __future__ import annotations

import dataclasses

import pytest

from exomem import dreamer_policy as policy


def _ready(**overrides) -> policy.TickSignals:
    base = policy.TickSignals(
        setting="on",
        standby=False,
        compute_mode="normal",
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
        ({"compute_mode": "quiet"}, "quiet_mode"),
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
    # A write every second for ten minutes: the vault never goes quiet, so no
    # decision in the burst may run, however idle the foreground is.
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


def test_no_max_wait_and_starvation_is_reported() -> None:
    # A day of continuous foreground use: the dreamer never forces a tick, and
    # every decision names the signal that blocked it.
    reasons = {
        policy.decide(_ready(idle_seconds=float(second % 30))).reason
        for second in range(0, 24 * 3600, 30)
    }
    assert reasons == {"foreground"}
    assert not hasattr(policy, "MAX_WAIT_SECONDS")


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
