"""Replace a small state file so a power cut leaves the old file or the new one, whole.

A cloud cell's hourly backup copies a block snapshot of its volume, which is the
state a power cut would leave (move-cloud-cells-to-local-storage D9). A file
written to a temporary name and renamed over the old one without a sync can
come back empty after that, and a truncated config silently resets a tenant's
settings. Syncing the new content before the rename makes the guarantee
independent of the filesystem's mount options.
"""

from __future__ import annotations

import contextlib
import os
import uuid
from pathlib import Path


def replace_text(path: Path, text: str, *, encoding: str = "utf-8", newline: str | None = None) -> None:
    """Write `text` to a sibling temporary file, sync it, then rename it over `path`.

    The rename is durable only once the directory is synced too; a caller
    that needs that (governance `durable_json`) syncs it after this returns.
    """

    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with open(temporary, "w", encoding=encoding, newline=newline) as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        with contextlib.suppress(OSError):
            temporary.unlink(missing_ok=True)
        raise
