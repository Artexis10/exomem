"""The dreamer's idle gate and budgets, as a pure decision.

The dreamer is a background worker that proposes bounded upkeep (the product
noun for what it hands the agent) off the interactive path. This module decides
WHEN it may work and HOW MUCH, and nothing else: `decide` maps one snapshot of
cheap probes to a `Decision`, with no I/O, no clock of its own and no state.

The rule is strict priority. Upkeep is the least important work in the
service: a person mid-conversation, a write burst, graph convergence and a
derived-index backlog all outrank it, and nothing online ever waits for it. So
there is deliberately no max-wait. The graph drain needs one because readers
wait on graph convergence; a dreamer starved by continuous use simply runs at
night, and the worker reports the blocking signal and `waiting_since` so the
starvation is visible rather than silent.

Every number states what it prevents. Constants live here with no environment
knob: a tunable nobody reviews is a control nobody can reason about.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

#: The operator settings. `off` (the default) means no thread and no sidecar;
#: `paused` keeps the thread and its checkpoint but runs no tick.
SETTINGS: tuple[str, ...] = ("off", "on", "paused")
DEFAULT_SETTING = "off"

#: Seconds since the last foreground request before a tick may start. Prevents
#: competing with a person mid-conversation.
IDLE_SECONDS = 60.0

#: The floor of the quiet window after any vault change. The window is the
#: longer of this and the last whole-vault graph pass, so graph repair always
#: gets the quiet moment first.
SETTLE_FLOOR_SECONDS = 60.0

#: Pages per tick. Half the graph drain's `DRAIN_LIMIT`, because this work
#: matters less; too low only makes the first reseed slower.
PAGES_PER_TICK = 32

#: Thread CPU per tick (`time.thread_time`). At most four of the foreground
#: checkpoint's 50 ms pause budgets, so a tick cannot monopolise the GIL.
TICK_CPU_SECONDS = 0.200

#: Wall time per tick. Bounds I/O-bound stalls on a slow disk.
TICK_WALL_SECONDS = 1.0

#: Duty cycle: sleep at least max(2 s, 4 x the tick's wall time) between ticks,
#: about 20% of one core at most in a burst.
MIN_SLEEP_SECONDS = 2.0
DUTY_FACTOR = 4.0

#: Thread CPU per rolling hour (1.7% of a core). Keeps a slow or pathological
#: vault from turning the dreamer into steady load on a shared host.
HOURLY_CPU_SECONDS = 60.0
CPU_WINDOW_SECONDS = 3600.0

#: How often a waiting worker re-reads its setting and probes the gate.
POLL_SECONDS = 30.0

#: Failure backoff: after this many consecutive failed ticks, wait
#: 2^n x 60 s (n counted past the threshold), capped at one hour.
BACKOFF_AFTER = 3
BACKOFF_BASE_SECONDS = 60.0
BACKOFF_CAP_SECONDS = 3600.0

#: Health reports `failed` from this many consecutive failed ticks.
FAILED_AFTER = 3


@dataclass(frozen=True)
class TickSignals:
    """One loop's probes. Each is cheap and gathered once per loop."""

    setting: str
    standby: bool
    compute_mode: str
    pressure: bool | None
    #: Seconds since the last foreground request ended; 0 while one is live.
    idle_seconds: float
    #: Seconds since the vault's freshness generation last moved.
    vault_quiet_seconds: float
    last_whole_vault_pass: float | None
    graph_debt: bool
    full_upsert_backlog: int
    freshness_live: bool
    hour_cpu_used: float
    consecutive_failures: int
    seconds_since_failure: float | None


@dataclass(frozen=True)
class Decision:
    run: bool
    reason: str
    page_budget: int
    sleep_s: float


def resolve_setting(env_value: str | None, config: dict[str, Any] | None) -> str:
    """`EXOMEM_DREAMER`, else the config key `dreamer`, else `off`.

    An unknown value at either tier falls through to the next rather than
    enabling anything: the safe reading of a typo is the default.
    """
    for raw in (env_value, (config or {}).get("dreamer")):
        value = str(raw or "").strip().lower()
        if value in SETTINGS:
            return value
    return DEFAULT_SETTING


def settle_seconds(last_whole_vault_pass: float | None) -> float:
    """The quiet window after a vault change: max(60 s, the last whole-vault pass)."""
    return max(SETTLE_FLOOR_SECONDS, float(last_whole_vault_pass or 0.0))


def backoff_seconds(consecutive_failures: int) -> float:
    """Wait after repeated failures: 0 below the threshold, then doubling, capped."""
    if consecutive_failures < BACKOFF_AFTER:
        return 0.0
    exponent = consecutive_failures - BACKOFF_AFTER
    if exponent >= 16:
        return BACKOFF_CAP_SECONDS
    return min(BACKOFF_CAP_SECONDS, BACKOFF_BASE_SECONDS * (2**exponent))


def sleep_after_tick(wall_seconds: float) -> float:
    """The duty cycle: at least max(2 s, 4 x the tick's wall time)."""
    return max(MIN_SLEEP_SECONDS, DUTY_FACTOR * max(0.0, float(wall_seconds)))


def tick_exhausted(*, pages: int, cpu: float, wall: float) -> str | None:
    """Which in-tick budget is spent, or None while the tick may continue."""
    if pages >= PAGES_PER_TICK:
        return "pages"
    if cpu >= TICK_CPU_SECONDS:
        return "cpu"
    if wall >= TICK_WALL_SECONDS:
        return "wall"
    return None


def _wait(reason: str, remaining: float | None = None) -> Decision:
    sleep = POLL_SECONDS
    if remaining is not None:
        sleep = min(POLL_SECONDS, max(1.0, remaining))
    return Decision(False, reason, 0, sleep)


def decide(signals: TickSignals) -> Decision:
    """Whether a tick may run now, and why not when it may not.

    Checked in a fixed order so the reported reason is stable: operator
    posture first, then host posture, then the foreground, then the vault and
    the graph, then the dreamer's own budgets.
    """
    if signals.setting not in {"on", "paused"}:
        return _wait("off")
    if signals.setting == "paused":
        return _wait("paused")
    if signals.standby:
        return _wait("standby")
    if signals.compute_mode == "quiet":
        return _wait("quiet_mode")
    if signals.pressure is True:
        return _wait("pressure")
    if signals.idle_seconds < IDLE_SECONDS:
        return _wait("foreground", IDLE_SECONDS - signals.idle_seconds)
    if not signals.freshness_live:
        return _wait("freshness_unavailable")
    settle = settle_seconds(signals.last_whole_vault_pass)
    if signals.vault_quiet_seconds < settle:
        return _wait("settling", settle - signals.vault_quiet_seconds)
    if signals.graph_debt:
        return _wait("graph_debt")
    if signals.full_upsert_backlog > 0:
        return _wait("index_backlog")
    if signals.hour_cpu_used >= HOURLY_CPU_SECONDS:
        return _wait("budget_exhausted")
    backoff = backoff_seconds(signals.consecutive_failures)
    if backoff > 0.0:
        since = signals.seconds_since_failure
        if since is None or since < backoff:
            return _wait("backoff", backoff - (since or 0.0))
    return Decision(True, "ready", PAGES_PER_TICK, 0.0)
