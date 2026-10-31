"""Which media kinds remote workers may lease, read from the per-vault media configuration (D7).

The block is the `remote_workers` key of the file that holds the media store policy. A vault
without it, or with an invalid one, routes nothing, so a typo never stops local work.
"""

from __future__ import annotations

import functools
import logging
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from . import media_brakes, media_engines, media_jobs, media_worker_credentials, originals

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


def covered_kinds(capabilities: object) -> frozenset[str]:
    """The media kinds that a worker's declared capabilities cover: the kinds whose engine it declared."""
    if not isinstance(capabilities, dict):
        return frozenset()
    kinds, engines = capabilities.get("media_types"), capabilities.get("engines")
    if not isinstance(kinds, list) or not isinstance(engines, list):
        return frozenset()
    covered = set()
    for kind in kinds:
        engine = media_engines.engine_for(kind) if isinstance(kind, str) else None
        if engine is not None and engine in engines:
            covered.add(kind)
    return frozenset(covered)


def present_kinds(
    routing: RemoteWorkers, credentials: Iterable[media_worker_credentials.WorkerCredential], now: float
) -> frozenset[str]:
    """The routed kinds that a present worker covers.

    A worker is present while its credential is live and was heard from within the presence duration, and it
    covers what it declared at its last claim. A worker that never declared anything covers nothing.
    """
    covered: set[str] = set()
    for credential in credentials:
        if (
            credential.status(now) == "active"
            and credential.last_seen_at is not None
            and now - credential.last_seen_at <= routing.presence_seconds
        ):
            covered |= covered_kinds(credential.capabilities)
    return routing.media_types & covered


def local_skip(vault: Path, store: media_jobs.MediaJobStore) -> frozenset[str]:
    """The media kinds that the local worker leaves to remote workers now.

    A kind is left while a present worker covers it, and always when local fallback is off. A job that no
    remote worker can take is the exception, which the claim makes with `routed_media_types`.
    """
    routing = remote_workers(vault)
    if routing is None or not routing.media_types:
        return frozenset()
    if not routing.local_fallback:
        return routing.media_types
    return present_kinds(routing, media_worker_credentials.list_credentials(store), time.time())


def sweep_leases(store: media_jobs.MediaJobStore, vault: Path) -> int:
    """Return the jobs of lapsed leases to the queue and forget old result outcomes; the number of rows changed.

    A claim runs it, and so does the media supervisor, so a vault that no worker calls still takes its
    jobs back.
    """
    config = media_brakes.settings()
    returned = len(store.expire_leases(stop_limit=config.stop_limit, context=media_brakes.timeout_context(config)))
    routing = remote_workers(vault)
    return returned + store.prune_outcomes(
        routing.outcome_seconds if routing is not None else media_jobs.DEFAULT_OUTCOME_SECONDS
    )
