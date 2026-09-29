"""Stat-generation cache for Records item files.

A Records snapshot is the container guard: it needs every item's bytes, digest and
parsed frontmatter. Reading, hashing and YAML-parsing every item on every append and
every guard refresh made both O(items). This cache keeps, per item file, the bytes
and digest read at one *stat generation* -- (device, inode, size, mtime, ctime) --
and serves them again while the file still has that generation. A file whose
generation moved (an edit from Obsidian, a rewrite by another tool, our own write)
is re-read and re-hashed; nothing else is.

Two rules keep it honest:

* **Racy window.** Kernel timestamps have finite granularity, so a write in the same
  tick as our read leaves the generation unchanged. A generation is trusted only if
  both timestamps were already older than `RACY_WINDOW_NS` when the bytes were read
  (the same rule git applies to its index). A recently written file is re-read by
  content until it ages out.
* **Untrusted platforms.** Where `st_ctime` does not move on a write (Windows) no
  generation is trusted and every read is by content, as before.

The parsed frontmatter is memoised by content digest, which is content-addressed and
so never stale.
"""

from __future__ import annotations

import hashlib
import os
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import vault

RACY_WINDOW_NS = 2_000_000_000
MAX_CACHED_BYTES = 64 * 1024 * 1024
MAX_CACHED_ITEMS = 16_384
MAX_PARSED = 16_384


@dataclass(frozen=True, slots=True)
class CachedItem:
    generation: tuple[int, int, int]
    identity: tuple[int | None, int | None, int]
    data: bytes
    digest: str
    guard: vault.PathGuard


_LOCK = threading.Lock()
_ITEMS: OrderedDict[tuple[str, str], CachedItem] = OrderedDict()
_BYTES = 0
_PARSED: OrderedDict[str, Any] = OrderedDict()


def clear() -> None:
    global _BYTES
    with _LOCK:
        _ITEMS.clear()
        _PARSED.clear()
        _BYTES = 0


def _identity(info: os.stat_result) -> tuple[int | None, int | None, int]:
    return getattr(info, "st_dev", None), getattr(info, "st_ino", None), info.st_mode


def _settled(info: os.stat_result, now_ns: int) -> bool:
    return now_ns - max(info.st_mtime_ns, info.st_ctime_ns) > RACY_WINDOW_NS


def read_item(root: Path, relative: str, *, limit: int) -> tuple[bytes, str, vault.PathGuard]:
    """Return (bytes, sha256, guard) for one item, re-reading only if its generation moved."""
    global _BYTES
    path = Path(root) / relative
    key = (str(root), relative)
    before: os.stat_result | None = None
    if vault.STAT_GENERATION_TRUSTED:
        try:
            before = path.lstat()
        except OSError:
            before = None
        if before is not None:
            with _LOCK:
                cached = _ITEMS.get(key)
                if (
                    cached is not None
                    and cached.generation == vault.stat_generation(before)
                    and cached.identity == _identity(before)
                ):
                    _ITEMS.move_to_end(key)
                    return cached.data, cached.digest, cached.guard
    data, guard = vault.read_bounded_guarded_bytes(Path(root), relative, limit=limit)
    digest = hashlib.sha256(data).hexdigest()
    if before is None or len(data) > limit:
        return data, digest, guard
    try:
        after = path.lstat()
    except OSError:
        return data, digest, guard
    generation = vault.stat_generation(before)
    if (
        vault.stat_generation(after) != generation
        or _identity(after) != _identity(before)
        or len(data) != before.st_size
        or not _settled(before, time.time_ns())
    ):
        return data, digest, guard
    try:
        stat_guard = vault.PathGuard.capture(
            Path(root), relative, leaf_policy="generation", expected_generation=generation
        )
    except vault.PathGuardError:
        return data, digest, guard
    with _LOCK:
        previous = _ITEMS.pop(key, None)
        if previous is not None:
            _BYTES -= len(previous.data)
        _ITEMS[key] = CachedItem(generation, _identity(before), data, digest, stat_guard)
        _BYTES += len(data)
        while _ITEMS and (_BYTES > MAX_CACHED_BYTES or len(_ITEMS) > MAX_CACHED_ITEMS):
            _key, evicted = _ITEMS.popitem(last=False)
            _BYTES -= len(evicted.data)
    return data, digest, stat_guard


def parsed_frontmatter(digest: str, text: str) -> tuple[dict[str, Any], str, str | None]:
    """`vault.parse_frontmatter(text, strict=True)`, memoised on the content digest.

    Callers treat the result as read-only. A malformed item raises and is not cached.
    """
    with _LOCK:
        hit = _PARSED.get(digest)
        if hit is not None:
            _PARSED.move_to_end(digest)
            return hit
    result = vault.parse_frontmatter(text, strict=True)
    with _LOCK:
        _PARSED[digest] = result
        while len(_PARSED) > MAX_PARSED:
            _PARSED.popitem(last=False)
    return result
