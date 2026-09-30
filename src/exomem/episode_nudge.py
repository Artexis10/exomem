"""The `episode_due` advisory: a tool-only client is asked to record, never forced.

A client with a Stop hook is asked to record an episode by that hook. claude.ai,
ChatGPT and a generic MCP client have none, so the one call they make every
turn -- `activate_context` -- carries the ask instead: after
`EPISODE_NUDGE_ACTIVATIONS` activations from one caller with no
`episode_memory` record, the packet gets a one-sentence `episode_due` block, at
most once per `EPISODE_NUDGE_COOLDOWN_SECONDS`. Claude Code and Codex over MCP
are asked too, unless this vault's activation log shows their hook served them
within that window: then their Stop hook is asking, and twice is noise. With
only the MCP server, this advisory is the only ask they get.

Governed like the capture-sweep advisory whose caller key it reuses
(`capture_sweep.ledger_key`): the agent decides whether anything durable
happened; the advisory is silent under `proactive_capture=off` (fail-closed);
it is never offered on REST or the CLI, which carry no MCP transport and are
the doors the hooks use; and its state lives in memory only, bounded, so a
restart re-arms each caller once. The one disk read is the activation log's
tail, for a Claude Code or Codex caller, at most once per cooldown. Two tabs of
one client share a key, which an advisory can afford.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Any

from . import query_log

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


def _hook_label(name: str | None) -> str | None:
    """The hook's client label for a Claude Code or Codex MCP client, else None.

    The same client-name rule `prominence.detect_surface` applies, mapped to
    the label the hooks declare on their REST and CLI rungs.
    """
    folded = (name or "").strip().casefold()
    if "codex" in folded:
        return "codex"
    if "claude-code" in folded or "claude code" in folded:
        return "claude-code"
    return None


def _caller(vault_root: Path | None) -> tuple[tuple[str, str, str], str | None] | None:
    """The caller's key and hook label on the MCP door, or None anywhere else."""
    try:
        from . import capture_sweep
        from .command_surface import mcp_caller_identity

        identity = mcp_caller_identity()
        if identity.get("transport") is None:
            return None
        key = capture_sweep.ledger_key(vault_root)
        return (key, _hook_label(identity.get("client_name"))) if key is not None else None
    except Exception:  # noqa: BLE001 - an unreadable caller is not advised
        log.debug("episode advisory caller identity unavailable", exc_info=True)
        return None


def on_activation(vault_root: Path | None) -> dict[str, Any] | None:
    """Count one activation and return the advisory when it is due. Never raises."""
    try:
        caller = _caller(vault_root)
        if caller is None:
            return None
        key, hook_label = caller
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
        if not due:
            return None
        # Outside the lock, and at most once per cooldown per caller: the
        # cooldown started above either way.
        if (
            hook_label is not None
            and vault_root is not None
            and query_log.hook_activation_seen(
                vault_root, hook_label, within_seconds=EPISODE_NUDGE_COOLDOWN_SECONDS
            )
        ):
            return None
        return {"rule": EPISODE_RULE, "activations": count}
    except Exception:  # noqa: BLE001 - an advisory never breaks a packet
        log.debug("episode advisory failed (non-fatal)", exc_info=True)
        return None


def note_record(vault_root: Path | None) -> None:
    """A recorded episode resets this caller's count and cooldown. Never raises."""
    caller = _caller(vault_root)
    if caller is None:
        return
    with _LOCK:
        _STATE.pop(caller[0], None)


def tracked_keys() -> int:
    with _LOCK:
        return len(_STATE)


def reset_state() -> None:
    """Forget every caller. For tests, and what a restart does."""
    with _LOCK:
        _STATE.clear()
