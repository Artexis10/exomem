"""The vault-resident record of bulk relayouts: the second durable source of prior paths.

A bulk relayout moves many pages without writing `moved_from` into each one, because that
would multiply its write cost. It records one row per moved page instead: the page's
`exomem_id` or none, its old path and its new path. The prior-path index folds these rows
into each page's claims. A row of a relayout that a later revert reversed claims nothing.

The record's writer belongs to the layout change; this module only reads it. The record is
`_Schema/relayout-record.yaml`:

    schema_version: 1
    relayouts:
      <relayout id>:
        reverted: false
        rows:
          - {exomem_id: <id or null>, old_path: <path>, new_path: <path>}
"""

from __future__ import annotations

import hashlib
import threading
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

from . import vault

RECORD_NAME = "relayout-record.yaml"
_SCHEMA_VERSION = 1


def record_path(vault_root: Path) -> Path:
    return vault.kb_root(Path(vault_root)) / vault.SCHEMA_OVERLAY_DIRNAME / RECORD_NAME


def record_rel(vault_root: Path) -> str:
    return record_path(vault_root).relative_to(Path(vault_root)).as_posix()


def _clean(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    clean = value.replace("\\", "/").strip().lstrip("/")
    return clean or None


@dataclass(frozen=True)
class Relayouts:
    """The rows that still claim, indexed by `exomem_id` and by new path for the refill."""

    by_id: dict[str, tuple[str, ...]] = field(default_factory=dict)
    by_new_path: dict[str, tuple[str, ...]] = field(default_factory=dict)

    def claims(self, exomem_id: str | None, held: Iterable[str]) -> Iterator[str]:
        """The old paths that rows claim for one page: by its id, then by a path it holds."""
        if exomem_id is not None:
            yield from self.by_id.get(exomem_id, ())
        for path in held:
            yield from self.by_new_path.get(path, ())

    def __bool__(self) -> bool:
        return bool(self.by_id or self.by_new_path)


EMPTY = Relayouts()


def parse(text: str | None) -> Relayouts:
    """The claiming rows of a record's text. A malformed record claims nothing.

    No check reports a malformed record yet; resolution keeps working on the pages' own
    lineage.
    """
    from . import memory_refs

    if not text:
        return EMPTY
    try:
        document = vault.yaml.safe_load(text)
    except vault.yaml.YAMLError:
        return EMPTY
    if not isinstance(document, dict) or document.get("schema_version") != _SCHEMA_VERSION:
        return EMPTY
    relayouts = document.get("relayouts")
    if not isinstance(relayouts, dict):
        return EMPTY
    by_id: dict[str, list[str]] = {}
    by_new_path: dict[str, list[str]] = {}
    for relayout in relayouts.values():
        if not isinstance(relayout, dict) or relayout.get("reverted") is True:
            continue
        rows = relayout.get("rows")
        for row in rows if isinstance(rows, list) else ():
            if not isinstance(row, dict):
                continue
            old, new = _clean(row.get("old_path")), _clean(row.get("new_path"))
            if old is None or new is None or old == new:
                continue
            identity = memory_refs.normalize_id(row.get("exomem_id"))
            if identity is not None:
                by_id.setdefault(identity, []).append(old)
            elif row.get("exomem_id") is None:
                by_new_path.setdefault(new, []).append(old)
    return Relayouts(
        {key: tuple(dict.fromkeys(value)) for key, value in by_id.items()},
        {key: tuple(dict.fromkeys(value)) for key, value in by_new_path.items()},
    )


_CACHE: dict[str, tuple[object, Relayouts]] = {}
_CACHE_LOCK = threading.Lock()


def _parsed(key: str, raw: bytes | None) -> Relayouts:
    """`parse` of record bytes, cached by their digest: a large record parses once per content."""
    if raw is None:
        return EMPTY
    digest = hashlib.blake2b(raw, digest_size=16).digest()
    with _CACHE_LOCK:
        cached = _CACHE.get(key)
    if cached is not None and cached[0] == digest:
        return cached[1]
    parsed = parse(raw.decode("utf-8", errors="replace"))
    with _CACHE_LOCK:
        _CACHE[key] = (digest, parsed)
    return parsed


def load(vault_root: Path, read_bytes: Callable[[str], bytes | None] | None = None) -> Relayouts:
    """The record's claiming rows.

    `read_bytes(rel)` reads the record where the caller's transaction sees it (a store
    commit), and the store serves it otherwise; both are cached by the record's digest. A
    record on disk is cached by its file signature, so a refill of many pages parses it once.
    """
    root = Path(vault_root)
    rel = record_rel(root)
    if read_bytes is not None:
        return _parsed(f"bytes:{root}", read_bytes(rel))
    from .vault_store import read

    def served() -> tuple[bytes | None]:
        try:
            return (read.owned_bytes(root, rel),)
        except FileNotFoundError:
            return (None,)

    # A one-tuple tells a served record the store lacks (no relayout) from a path it does not serve.
    stored = read.from_store(root, rel, served)
    if stored is not None:
        return _parsed(f"bytes:{root}", stored[0])
    path = root / rel
    try:
        info = path.stat()
    except (FileNotFoundError, NotADirectoryError):
        return EMPTY
    signature = (info.st_mtime_ns, info.st_size, info.st_ino)
    key = str(path)
    with _CACHE_LOCK:
        cached = _CACHE.get(key)
    if cached is not None and cached[0] == signature:
        return cached[1]
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return EMPTY
    parsed = parse(text)
    with _CACHE_LOCK:
        _CACHE[key] = (signature, parsed)
    return parsed
