"""The write-ahead record of a binary that adoption renamed into the Knowledge Base before its Source page committed.

Between the rename and the commit the binary sits unbound at its final path, and a stop leaves it there
until the next `apply` replays the member. The file watcher and the media scans must not bind it in
that time: a pending sidecar at the Source page's path would stop the page from being created. A run
must not be cancelled, re-planned or restarted in that time either, because the file would then belong
to no run.

A record is one small file in the vault's state folder per renamed binary. It is written, and flushed,
before the chunk's renames. It is removed once the Source page commits or the file goes back to its
stage. A file that the run could neither bind nor put back keeps its record until the person moves or
deletes it: a record whose file is gone, replaced or bound no longer holds. It names the renamed file by
inode, so a different file that later appears at the same path is not held, and it names the import, the
run and the staged path, so a caller can find the in-flight renames of a run, or of a stage, without its plan.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from . import private_state, state_paths

STORE = "import-renames"
#: A record's temp file lives for the length of one write; one this old was left by a stop.
STALE_TEMP_SECONDS = 3600


@dataclass(frozen=True, slots=True)
class Hold:
    """One renamed binary that no committed Source page binds yet."""

    #: The binary's vault-relative path, which names the record.
    path: str
    inode: int
    import_id: str
    run_id: str
    #: Where the binary was staged: the path the run planned it under.
    staged_path: str


def _stem(relative_path: str) -> str:
    """The record's file name: a fixed-length digest, so no path character reaches the state folder."""
    return hashlib.sha256(relative_path.encode("utf-8")).hexdigest()


def _store_path(vault_root: Path) -> Path:
    return state_paths.vault_state_dir(vault_root) / STORE


def hold_all(vault_root: Path, holds: Sequence[Hold]) -> None:
    """Record, before the renames, that each hold's file is about to appear unbound at its path.

    Every record is flushed, and the store's folder once after them, so a stop or a power loss after
    the first rename cannot lose the record that the rename depends on.
    """
    store = private_state.store(vault_root, STORE)
    _sweep(store)
    for hold in holds:
        private_state.write_record(store, _stem(hold.path), dataclasses.asdict(hold), temp_suffix=".tmp", durable=True)
    private_state.sync_directory(store)


def release(vault_root: Path, relative_path: str) -> None:
    """Drop the record once the path is bound, or the file is back in its stage."""
    try:
        private_state.remove(_store_path(vault_root) / f"{_stem(relative_path)}.json")
    except ValueError:
        return


def _read(record_path: Path) -> Hold | None:
    try:
        return Hold(**json.loads(record_path.read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError):
        return None


def _holds(vault_root: Path) -> list[Hold]:
    try:
        records = sorted(_store_path(vault_root).glob("*.json"))
    except (OSError, ValueError):
        return []
    return [hold for hold in map(_read, records) if hold is not None]


def _live(vault_root: Path, hold: Hold) -> bool:
    """Whether the file the record names is still the unbound binary: same inode, and no sidecar page beside it.

    A sidecar means a page binds the file, so the hold has done its work. This is what lets a stop
    between the page's commit and the record's release heal itself.
    """
    from . import originals

    binary = vault_root / hold.path
    try:
        return os.stat(binary).st_ino == hold.inode and not originals.sidecar_path(binary).exists()
    except OSError:
        return False


def is_held(vault_root: Path, binary: Path) -> bool:
    """Whether `binary` is a renamed file that no committed Source page binds yet.

    Any read problem answers no. A record is written whole, so a damaged one is not a case to plan for,
    and holding every binary of the vault back would cost more than one wrongly bound file.
    """
    try:
        relative_path = Path(binary).relative_to(vault_root).as_posix()
    except ValueError:
        return False
    hold = _read(_store_path(vault_root) / f"{_stem(relative_path)}.json")
    return hold is not None and _live(vault_root, hold)


def pending(vault_root: Path, *, run_id: str | None = None, under: str | None = None) -> list[Hold]:
    """The live holds of one run, or of the files staged under a vault-relative folder; every live hold when neither is given.

    A record whose file is gone or bound is not pending, so it never blocks a caller.
    """
    prefix = None if under is None else under.strip("/")
    return [
        hold for hold in _holds(vault_root)
        if (run_id is None or hold.run_id == run_id)
        and (not prefix or hold.staged_path == prefix or hold.staged_path.startswith(f"{prefix}/"))
        and _live(vault_root, hold)
    ]


def _sweep(store: Path) -> None:
    """Remove the temp file of a record write that a stop cut short."""
    now = time.time()
    for leftover in store.glob("*.tmp"):
        try:
            stale = leftover.stat().st_mtime + STALE_TEMP_SECONDS <= now
        except OSError:
            continue
        if stale:
            private_state.remove(leftover)
