"""Optional automatic quiet-mode switching from non-torch pressure signals."""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Literal

from . import mode, resource_status

log = logging.getLogger(__name__)

_ENABLED_ENV = "EXOMEM_AUTO_QUIET"
_POLL_ENV = "EXOMEM_AUTO_QUIET_POLL_SECONDS"
_ENTER_ENV = "EXOMEM_AUTO_QUIET_ENTER_SECONDS"
_RESTORE_ENV = "EXOMEM_AUTO_QUIET_RESTORE_SECONDS"

_DEFAULT_POLL_SECONDS = 5.0
_DEFAULT_ENTER_SECONDS = 30.0
_DEFAULT_RESTORE_SECONDS = 60.0

Action = Literal["none", "enter_quiet", "restore"]


@dataclass
class AutoQuietState:
    pressure_since: float | None = None
    clear_since: float | None = None
    previous_mode: str | None = None
    engaged: bool = False


@dataclass(frozen=True)
class AutoQuietDecision:
    action: Action
    target_mode: str | None = None
    reason: str = ""


def _truthy(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() not in {"", "0", "false", "no", "off"}


def enabled() -> bool:
    env = os.environ.get(_ENABLED_ENV)
    if env is not None:
        return _truthy(env)
    return _truthy(mode.read_config().get("auto_quiet"))


def _float_env(name: str, default: float) -> float:
    try:
        return max(0.0, float(os.environ.get(name) or default))
    except ValueError:
        return default


def pressure_active() -> bool | None:
    """Return True under pressure, False when clear, None when probe is unavailable."""
    try:
        gpu = resource_status.gpu_headroom()
    except Exception:  # noqa: BLE001
        log.debug("auto-quiet pressure probe failed", exc_info=True)
        return None
    status = gpu.get("status")
    if status == "marginal":
        return True
    if status in {"capable", "disabled"}:
        return False
    return None


def decide(
    state: AutoQuietState,
    *,
    current_mode: str,
    config_mode: str | None,
    pressure: bool | None,
    now: float,
    env_pinned: bool,
    enter_after: float = _DEFAULT_ENTER_SECONDS,
    restore_after: float = _DEFAULT_RESTORE_SECONDS,
) -> AutoQuietDecision:
    """Pure hysteresis decision; mutates only the supplied state object."""
    current_mode = mode.normalize(current_mode) or "normal"
    config_mode = mode.normalize(config_mode) or current_mode
    if env_pinned:
        return AutoQuietDecision("none", reason="EXOMEM_MODE pins mode")
    if pressure is None:
        state.pressure_since = None
        return AutoQuietDecision("none", reason="pressure probe unavailable")

    if state.engaged and current_mode != "quiet":
        state.engaged = False
        state.previous_mode = None
        state.pressure_since = None
        state.clear_since = None
        return AutoQuietDecision("none", reason="manual mode change detected")

    if pressure:
        state.clear_since = None
        if current_mode == "quiet":
            return AutoQuietDecision("none", reason="already quiet")
        if state.pressure_since is None:
            state.pressure_since = now
            return AutoQuietDecision("none", reason="pressure hysteresis started")
        if now - state.pressure_since < enter_after:
            return AutoQuietDecision("none", reason="pressure hysteresis pending")
        state.previous_mode = config_mode if config_mode != "quiet" else "normal"
        state.engaged = True
        state.pressure_since = None
        return AutoQuietDecision("enter_quiet", target_mode="quiet", reason="pressure sustained")

    state.pressure_since = None
    if not state.engaged:
        state.clear_since = None
        return AutoQuietDecision("none", reason="pressure clear")
    if current_mode != "quiet":
        state.engaged = False
        state.previous_mode = None
        state.clear_since = None
        return AutoQuietDecision("none", reason="manual mode change detected")
    if state.clear_since is None:
        state.clear_since = now
        return AutoQuietDecision("none", reason="restore hysteresis started")
    if now - state.clear_since < restore_after:
        return AutoQuietDecision("none", reason="restore hysteresis pending")
    target = state.previous_mode or "normal"
    state.engaged = False
    state.previous_mode = None
    state.clear_since = None
    return AutoQuietDecision("restore", target_mode=target, reason="pressure clear")


_STATE = AutoQuietState()
_THREAD: threading.Thread | None = None
_STOP = threading.Event()
_LOCK = threading.Lock()


def tick(state: AutoQuietState | None = None, *, now: float | None = None) -> AutoQuietDecision:
    state = state or _STATE
    now = time.monotonic() if now is None else now
    cfg = mode.read_config()
    decision = decide(
        state,
        current_mode=mode.resolve_mode(),
        config_mode=cfg.get("mode"),
        pressure=pressure_active(),
        now=now,
        env_pinned=bool(os.environ.get("EXOMEM_MODE")),
        enter_after=_float_env(_ENTER_ENV, _DEFAULT_ENTER_SECONDS),
        restore_after=_float_env(_RESTORE_ENV, _DEFAULT_RESTORE_SECONDS),
    )
    if decision.action in {"enter_quiet", "restore"} and decision.target_mode:
        mode.write_mode(decision.target_mode)
        try:
            mode.apply_live()
        except Exception:  # noqa: BLE001
            log.debug("auto-quiet live apply failed", exc_info=True)
        log.info("auto-quiet %s -> %s (%s)", decision.action, decision.target_mode, decision.reason)
    return decision


def _run() -> None:
    while not _STOP.wait(_float_env(_POLL_ENV, _DEFAULT_POLL_SECONDS)):
        try:
            tick()
        except Exception:  # noqa: BLE001
            log.debug("auto-quiet tick failed", exc_info=True)


def start_if_enabled() -> threading.Thread | None:
    if not enabled():
        return None
    global _THREAD
    with _LOCK:
        if _THREAD is not None and _THREAD.is_alive():
            return _THREAD
        _STOP.clear()
        _THREAD = threading.Thread(target=_run, name="exomem-auto-quiet", daemon=True)
        _THREAD.start()
        return _THREAD


def stop() -> None:
    global _THREAD
    _STOP.set()
    with _LOCK:
        thread = _THREAD
        _THREAD = None
    if thread is not None:
        thread.join(timeout=2)
