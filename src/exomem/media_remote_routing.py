"""Which media kinds remote workers may lease, read from the per-vault media configuration (D7).

The block is the `remote_workers` key of the file that holds the media store policy. A vault
without it, or with an invalid one, routes nothing, so a typo never stops local work.
"""

from __future__ import annotations

import functools
import logging
from dataclasses import dataclass
from pathlib import Path

from . import media_engines, media_jobs, originals

log = logging.getLogger(__name__)

# Defaults for the timing keys. A worker's idle claim returns after 25 s, so a worker is present
# for longer than one claim; a heartbeat comes four times in a lease.
DEFAULT_LEASE_SECONDS = 120.0
DEFAULT_PRESENCE_SECONDS = 90.0


# The keys of the block, which this module's schema fixes. An unknown key is a typo to report, not a vocabulary.
_KEYS = frozenset(  # nosemgrep: ep-word-set -- the block's own schema keys.
    {"media_types", "local_fallback", "lease_seconds", "heartbeat_seconds", "presence_seconds", "outcome_seconds"}
)


@dataclass(frozen=True, slots=True)
class RemoteWorkers:
    media_types: frozenset[str]
    local_fallback: bool
    lease_seconds: float
    heartbeat_seconds: float
    presence_seconds: float
    outcome_seconds: float = media_jobs.DEFAULT_OUTCOME_SECONDS


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float) or not value > 0:
        return None
    return float(value)


def parse(block: object) -> RemoteWorkers | str:
    """The routing a `remote_workers` block configures, or the reason it is invalid."""
    if not isinstance(block, dict):
        return "remote_workers must be an object"
    unknown = sorted(set(block) - _KEYS)
    if unknown:
        return f"remote_workers has unknown keys: {', '.join(unknown)}"
    kinds = block.get("media_types", [])
    # Speech is the one engine a remote worker runs, so its kinds are the only ones that can be routed.
    if (
        not isinstance(kinds, list)
        or any(not isinstance(kind, str) or kind not in media_engines.SPEECH_KINDS for kind in kinds)
    ):
        return f"remote_workers.media_types must be a list drawn from {sorted(media_engines.SPEECH_KINDS)}"
    fallback = block.get("local_fallback", True)
    if not isinstance(fallback, bool):
        return "remote_workers.local_fallback must be true or false"
    lease = _number(block.get("lease_seconds", DEFAULT_LEASE_SECONDS))
    if lease is None:
        return "remote_workers.lease_seconds must be a positive number"
    heartbeat = _number(block.get("heartbeat_seconds", lease / 4))
    if heartbeat is None or heartbeat >= lease:
        return "remote_workers.heartbeat_seconds must be a positive number below lease_seconds"
    presence = _number(block.get("presence_seconds", DEFAULT_PRESENCE_SECONDS))
    if presence is None:
        return "remote_workers.presence_seconds must be a positive number"
    outcome = _number(block.get("outcome_seconds", media_jobs.DEFAULT_OUTCOME_SECONDS))
    if outcome is None:
        return "remote_workers.outcome_seconds must be a positive number"
    return RemoteWorkers(frozenset(kinds), fallback, lease, heartbeat, presence, outcome)


def remote_workers(vault: Path) -> RemoteWorkers | None:
    """The vault's remote worker routing, or None when it configures none or an invalid one."""
    try:
        entry = originals.vault_entry(vault)
    except (OSError, ValueError) as error:
        _warn_once(f"the media configuration cannot be read ({type(error).__name__})")
        return None
    if not isinstance(entry, dict) or originals.REMOTE_WORKERS_KEY not in entry:
        return None
    parsed = parse(entry[originals.REMOTE_WORKERS_KEY])
    if isinstance(parsed, str):
        _warn_once(parsed)
        return None
    return parsed


@functools.cache
def _warn_once(problem: str) -> None:
    # Every claim reads the block, so the cache keeps one warning per problem in each process.
    log.warning("remote media workers are off: %s", problem)
