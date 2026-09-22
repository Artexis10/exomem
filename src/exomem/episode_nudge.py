"""The `episode_due` advisory: a tool-only client is asked to record, never forced.

A client with a Stop hook is asked to record an episode by that hook. claude.ai,
ChatGPT and a generic MCP client have none, so the one call they make every
turn -- `activate_context` -- carries the ask instead: after
`EPISODE_NUDGE_ACTIVATIONS` activations from one caller with no
`episode_memory` record, the packet gets a one-sentence `episode_due` block, at
most once per `EPISODE_NUDGE_COOLDOWN_SECONDS`.

Governed like the capture-sweep advisory whose caller key it reuses
(`capture_sweep.ledger_key`): the agent decides whether anything durable
happened; the advisory is silent under `proactive_capture=off` (fail-closed);
it is never offered on REST or the CLI, which carry no MCP transport and are
the doors the hooks use; and it lives in memory only, bounded, so a restart
re-arms each caller once and never changes what an agent is told from disk.
Two tabs of one client share a key, which an advisory can afford.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

#: Activations since the caller's last record before the advisory may ride.
EPISODE_NUDGE_ACTIVATIONS = 8
#: The least time between two advisories to one caller.
EPISODE_NUDGE_COOLDOWN_SECONDS = 1800
#: One entry per caller key; the least recently active is evicted first.
LEDGER_CAP = 512

EPISODE_RULE = (
    "This conversation has run several turns without a recap. At its next "
    "decision or stopping point, record one with episode_memory: what was worked "
    "on, decided and left open. Skip it if nothing durable happened."
)

#: Monotonic and injectable, as in `capture_sweep`: the cooldown is an elapsed
#: duration, and a wall clock stepping backwards must not re-arm a caller.
_clock = time.monotonic

_LOCK = threading.Lock()
#: `key -> [activations since the last record, monotonic time of the last
#: advisory or None]`, least recently active first.
_STATE: dict[tuple[str, str, str], list[Any]] = {}


def _key(vault_root: Path | None) -> tuple[str, str, str] | None:
    """The caller's key on the MCP door, or None anywhere else."""
    try:
        from . import capture_sweep
        from .command_surface import mcp_caller_identity

        if mcp_caller_identity().get("transport") is None:
            return None
        return capture_sweep.ledger_key(vault_root)
    except Exception:  # noqa: BLE001 - an unreadable caller is not advised
        log.debug("episode advisory caller identity unavailable", exc_info=True)
        return None


def on_activation(vault_root: Path | None) -> dict[str, Any] | None:
    """Count one activation and return the advisory when it is due. Never raises."""
    try:
        key = _key(vault_root)
        if key is None:
            return None
        from . import capture_sweep

        permitted = capture_sweep._proactive_capture_permitted()  # noqa: SLF001
        now = float(_clock())
        with _LOCK:
            state = _STATE.pop(key, None)
            if state is None:
                state = [0, None]
                if len(_STATE) >= LEDGER_CAP:
                    _STATE.pop(next(iter(_STATE)))
            state[0] += 1
            due = (
                permitted
                and state[0] >= EPISODE_NUDGE_ACTIVATIONS
                and (state[1] is None or now - state[1] >= EPISODE_NUDGE_COOLDOWN_SECONDS)
            )
            if due:
                state[1] = now
            _STATE[key] = state
            count = state[0]
        return {"rule": EPISODE_RULE, "activations": count} if due else None
    except Exception:  # noqa: BLE001 - an advisory never breaks a packet
        log.debug("episode advisory failed (non-fatal)", exc_info=True)
        return None


def note_record(vault_root: Path | None) -> None:
    """A recorded episode resets this caller's count and cooldown. Never raises."""
    key = _key(vault_root)
    if key is None:
        return
    with _LOCK:
        _STATE.pop(key, None)


def tracked_keys() -> int:
    with _LOCK:
        return len(_STATE)


def reset_state() -> None:
    """Forget every caller. For tests, and what a restart does."""
    with _LOCK:
        _STATE.clear()
