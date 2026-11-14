"""Record the vault Markdown pages that a block of test code opens.

A Python audit hook sees every `open` in this process. A hook cannot be removed, so this module
installs one hook and records only inside a `pages_opened` block.

Bounds: it does not see a child process's reads, and it does not see a check that only stats a
path. A test that uses it proves which pages were read, not which were looked at.
"""

from __future__ import annotations

import contextlib
import os
import sys
from collections.abc import Iterator
from pathlib import Path

_recording: list[tuple[str, set[str]]] = []


def _audit(event: str, args: tuple) -> None:
    if event != "open" or not _recording or not args:
        return
    target = args[0]
    if not isinstance(target, (str, bytes, os.PathLike)):
        return  # a file descriptor names no path
    path = os.path.realpath(os.fsdecode(target))
    if not path.endswith(".md"):
        return
    for root, seen in _recording:
        if path.startswith(root):
            seen.add(path[len(root):].replace(os.sep, "/"))


sys.addaudithook(_audit)


@contextlib.contextmanager
def pages_opened(vault: Path) -> Iterator[set[str]]:
    """The vault-relative paths of the Markdown pages opened inside the block."""
    seen: set[str] = set()
    entry = (os.path.realpath(vault) + os.sep, seen)
    _recording.append(entry)
    try:
        yield seen
    finally:
        _recording.remove(entry)
