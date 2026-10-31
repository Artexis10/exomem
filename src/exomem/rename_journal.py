"""The write-ahead record of a binary that adoption renamed into the Knowledge Base before its Source page committed.

Between the rename and the commit the binary sits unbound at its final path, and a stop leaves it there
until the next `apply` replays the member. The file watcher and the media scans must not bind it in
that time: a pending sidecar at the Source page's path would stop the page from being created.

A record is one small file in the vault's state folder, written before the rename and removed once the
Source page commits or the file goes back. It names the renamed file by inode, so a different file that
later appears at the same path is not held.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from . import private_state, state_paths

STORE = "import-renames"


def _stem(relative_path: str) -> str:
    """The record's file name: a fixed-length digest, so no path character reaches the state folder."""
    return hashlib.sha256(relative_path.encode("utf-8")).hexdigest()


def hold(vault_root: Path, relative_path: str, inode: int) -> None:
    """Record, before the rename, that the file `inode` is about to appear unbound at `relative_path`."""
    private_state.write_record(
        private_state.store(vault_root, STORE), _stem(relative_path),
        {"path": relative_path, "inode": inode}, temp_suffix=".tmp",
    )


def release(vault_root: Path, relative_path: str) -> None:
    """Drop the record once the path is bound, or the file is back in its stage."""
    try:
        private_state.remove(state_paths.vault_state_dir(vault_root) / STORE / f"{_stem(relative_path)}.json")
    except ValueError:
        return


def is_held(vault_root: Path, binary: Path) -> bool:
    """Whether `binary` is a renamed file that no committed Source page binds yet.

    Any read problem answers no. A record is written whole, so a damaged one is not a case to plan for,
    and holding every binary of the vault back would cost more than one wrongly bound file.
    """
    try:
        relative_path = Path(binary).relative_to(vault_root).as_posix()
        record = json.loads(
            (state_paths.vault_state_dir(vault_root) / STORE / f"{_stem(relative_path)}.json")
            .read_text(encoding="utf-8")
        )
        return record["inode"] == os.stat(binary).st_ino
    except (OSError, ValueError, KeyError, TypeError):
        return False
