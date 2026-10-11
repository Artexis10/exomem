"""Owner-only stores under a vault's state directory.

Held uploads, upload sessions and the archive-expansion locks each keep one
directory here. A store is private (0700) whoever created it, and each record
lands whole through a rename, so a reader never sees half of one.
"""

from __future__ import annotations

import json
import os
import stat
import tempfile
from pathlib import Path


def store(vault_root: Path, dirname: str) -> Path:
    """The directory `dirname` under the vault's state directory, created and kept owner-only."""
    from .state_paths import ensure_vault_state_dir

    path = ensure_vault_state_dir(vault_root) / dirname
    path.mkdir(mode=0o700, exist_ok=True)
    # `mkdir` leaves an existing directory as it was; a store is private whoever created it.
    info = os.lstat(path)
    if not stat.S_ISDIR(info.st_mode):
        raise OSError(f"{dirname} store is not a directory: {path}")
    if os.name == "posix":
        if info.st_uid != os.geteuid():
            raise OSError(f"{dirname} store is owned by another user: {path}")
        if stat.S_IMODE(info.st_mode) != 0o700:
            os.chmod(path, 0o700)
    return path


def remove(*paths: Path) -> None:
    """Unlink each path; one that is gone or cannot be removed is left to its store's sweep."""
    for path in paths:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


def write_record(store: Path, stem: str, record: dict, *, temp_suffix: str) -> None:
    """Write `<stem>.json` whole through a temp named `<stem>.*<temp_suffix>`, which the store's sweep owns."""
    descriptor, raw = tempfile.mkstemp(prefix=f"{stem}.", suffix=temp_suffix, dir=store)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as sink:
            json.dump(record, sink)
        os.replace(raw, store / f"{stem}.json")
    except BaseException:
        remove(Path(raw))
        raise
