"""Launcher custody for original-storage and temporary-upload format changes."""

from __future__ import annotations

import threading
from pathlib import Path

from filelock import FileLock

from . import state_migration

# The worker command protocol fixes these IDs; they describe implemented formats.
LIFECYCLE_IDS = (
    state_migration.SHARED_ORIGINALS_COMPATIBILITY_ID,
    state_migration.HELD_UPLOAD_CANCELLATION_COMPATIBILITY_ID,
)

_supported: frozenset[str] = frozenset()
# One activation per vault and process; later cancellations skip the manifest entirely.
_ACTIVATION_WAIT_SECONDS = 2.0
_activated: set[Path] = set()
_lock = threading.Lock()


def claim_launcher_support(formats: list[str] | None) -> None:
    """Claim launcher-authored arguments before worker bootstrap, never environment values."""
    global _supported
    # A newer launcher may declare formats this release never wrote; they grant nothing here.
    _supported = frozenset(formats or ()) & frozenset(LIFECYCLE_IDS)


def claim_standalone_support() -> None:
    """A standalone server, including a Cloud cell, runs no older worker beside itself.

    An older release still refuses the recorded format until offline cleanup retires it.
    """
    global _supported
    _supported = frozenset(LIFECYCLE_IDS)


def supports(format_id: str) -> bool:
    from . import service_standby

    return format_id in _supported and not service_standby.in_standby()


def activate_cancellation(vault_root: Path) -> None:
    """Settle old ticket deletions before this worker can create cancellation markers."""
    from . import upload_handoff

    root = Path(vault_root).resolve()
    required = state_migration.HELD_UPLOAD_CANCELLATION_COMPATIBILITY_ID
    if not supports(required):
        raise state_migration.StateMigrationOfflineRequired(
            "update the managed launcher offline before cancelling held uploads"
        )
    if root in _activated:
        return
    # Bounded, like startup admission: a cancellation answers busy rather than waiting on a migrator.
    if not _lock.acquire(timeout=_ACTIVATION_WAIT_SECONDS):
        raise TimeoutError("artifact lifecycle activation")
    try:
        if root in _activated:
            return
        # The launcher excludes old workers; the ticket lock settles concurrent new claims.
        upload_handoff.settle_consumption(root)
        state_migration.record_held_upload_cancellation_compatibility(
            root, authority_check=lambda: supports(required), active=True,
            lock_timeout=_ACTIVATION_WAIT_SECONDS,
        )
        _activated.add(root)
    finally:
        _lock.release()


def cleanup_cancellations(vault_root: Path, *, authority: object, deadline: float) -> int:
    """Finish markers under an externally proven stop window, retaining failed custody."""
    import time

    from . import held_uploads

    state_migration._require_offline_authority(authority)
    store = held_uploads._store(vault_root)
    completed = 0
    with FileLock(str(store / ".lock"), timeout=0):
        for marker in store.glob(f"*{held_uploads._CANCELLING_SUFFIX}"):
            if time.monotonic() >= deadline:
                raise TimeoutError("held-upload cleanup budget exhausted; resume cleanup")
            key = held_uploads.marker_key(marker)
            if key is None:
                continue  # Not a file this store wrote; it owns no payload and blocks nothing.
            held_uploads._finish_cancellation(store, key, marker)
            completed += 1
        state_migration.record_held_upload_cancellation_compatibility(
            vault_root,
            authority_check=lambda: state_migration._require_offline_authority(authority) is authority,
            active=False,
        )
    return completed
