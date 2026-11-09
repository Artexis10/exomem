"""Write a stored page's bytes into a vault tree (OpenSpec make-vault-store-canonical D4).

Migration's proof, rollback and the projector share this one writer, so each puts a page on disk the same way:
a temporary file beside the target, made durable when asked, then renamed into place.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

from .. import vault

#: Beside a target while it is written; a startup sweep removes a leftover (D4).
TEMP_PREFIX = ".exomem-projecting-"


def write_file(target_root: Path, rel: str, body: bytes, *, durable: bool) -> Path:
    """Write ``body`` to ``target_root/rel`` and return the path; ``durable`` makes it survive a power loss.

    The file replaces any file at ``rel`` in one rename, so a reader sees the old bytes or the new ones.
    Durable means the temporary file and then its directory are flushed to disk before and after the rename.
    """
    target = Path(target_root) / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.parent / f"{TEMP_PREFIX}{uuid.uuid4().hex}"
    try:
        with open(temp, "wb") as handle:
            handle.write(body)
            handle.flush()
            if durable:
                os.fsync(handle.fileno())
        vault.replace_tolerating_transient_sharing(lambda: os.replace(temp, target))
    except BaseException:
        temp.unlink(missing_ok=True)
        raise
    if durable:
        _sync_directory(target.parent)
    return target


def _sync_directory(directory: Path) -> None:
    # Windows cannot open a directory for a flush; its rename is durable once the file's flush returned.
    if os.name == "nt":
        return
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
