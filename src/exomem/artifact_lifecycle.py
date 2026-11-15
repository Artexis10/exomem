"""Launcher custody for temporary-upload format changes."""

from __future__ import annotations

import threading
from pathlib import Path

from filelock import FileLock

from . import state_migration

# The worker command protocol fixes these IDs; they describe implemented formats.
LIFECYCLE_IDS = (state_migration.HELD_UPLOAD_CANCELLATION_COMPATIBILITY_ID,)

_supported: frozenset[str] = frozenset()
_activated: set[Path] = set()
_lock = threading.Lock()


def claim_launcher_support(formats: list[str] | None) -> None:
    """Claim launcher-authored arguments before worker bootstrap, never environment values."""
    global _supported
    requested = frozenset(formats or ())
    if requested - set(LIFECYCLE_IDS):
        raise ValueError("unsupported managed artifact lifecycle declaration")
    _supported = requested


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
    if not _lock.acquire(blocking=False):
        from filelock import Timeout

        raise Timeout("artifact lifecycle activation")
    try:
        if root in _activated:
            return
        # The launcher excludes old workers; the ticket lock settles concurrent new claims.
        upload_handoff.settle_consumption(root)
        state_migration.record_held_upload_cancellation_compatibility(
            root, authority_check=lambda: supports(required), active=True,
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
            key = marker.name.removesuffix(held_uploads._CANCELLING_SUFFIX)
            # Marker filenames use the closed SHA-256 storage-key format.
            if len(key) != 64 or any(character not in "0123456789abcdef" for character in key):
                raise ValueError("held-upload cancellation marker has an invalid storage key")
            held_uploads._finish_cancellation(store, key, marker)
            completed += 1
        state_migration.record_held_upload_cancellation_compatibility(
            vault_root,
            authority_check=lambda: state_migration._require_offline_authority(authority) is authority,
            active=False,
        )
    return completed
