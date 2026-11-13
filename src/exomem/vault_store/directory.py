"""Directory scans over canonical pages and the files the store does not own."""

from __future__ import annotations

import os
from dataclasses import dataclass
from itertools import islice
from pathlib import Path

from . import mode, ownership, read


def is_dir(root: Path, rel: str) -> bool:
    """A virtual stored folder exists before its directory projection does."""
    if mode.store_mode_for_reads(root):
        if read.holds_folder(root, rel):
            return True
        if read.serves(root, rel):
            return False
    return (root / rel).is_dir()


@dataclass(frozen=True)
class Entry:
    """A directory child, with stored metadata or its original filesystem entry."""

    path: Path
    directory: bool
    meta: read.PageMeta | None = None
    disk: os.DirEntry | None = None

    @property
    def name(self) -> str:
        return self.path.name


class Reader:
    """Merge directory names once; never inspect a page's projected leaf.

    Whole scans batch metadata and size-capped bytes. Bounded scans read only the requested
    directory entries and fetch their admitted bytes as a batch within the caller's byte budget.
    """

    def __init__(
        self, root: Path, *, whole: bool = False, max_page_bytes: int = 0, subtree: str = "",
    ) -> None:
        self.root = root
        self.whole = whole
        self._directories: dict[str, dict[str, Entry]] = {}
        self._candidates: list[Entry] = []
        self._positions: dict[str, int] = {}
        self._batch_size = 1
        if whole:
            with read.snapshot(root) as view:
                prefix = f"{subtree.rstrip('/')}/" if subtree and subtree != "." else ""
                metadata = tuple(view.pages(prefix))
                view.memo.setdefault("page_metadata", {}).update((meta.path, meta) for meta in metadata)
                view.prefetch_pages(
                    meta.path for meta in metadata
                    if meta.path.lower().endswith(".md") and meta.size <= max_page_bytes
                )
                for meta in metadata:
                    parts = meta.path.split("/")
                    for depth, name in enumerate(parts):
                        folder = "/".join(parts[:depth])
                        relative = "/".join(parts[:depth + 1])
                        self._directories.setdefault(folder, {})[name] = Entry(
                            root / relative, depth < len(parts) - 1,
                            meta if depth == len(parts) - 1 else None,
                        )

    def entries(self, directory: Path, limit: int | None = None) -> list[Entry]:
        rel = directory.relative_to(self.root).as_posix()
        rel = "" if rel == "." else rel
        if self.whole:
            stored = dict(self._directories.get(rel, {}))
        else:
            stored = self._stored_entries(rel, limit)
        try:
            # A virtual directory can have a missing or symlinked projection. Only its stored
            # children count there; following that projection could import unrelated disk files.
            expected = self.root.resolve() / directory.relative_to(self.root)
            if directory.resolve(strict=True) != expected:
                return sorted(stored.values(), key=lambda entry: entry.name)
            with os.scandir(directory) as scanned:
                for child in islice(scanned, limit):
                    child_rel = f"{rel}/{child.name}" if rel else child.name
                    if child.name in stored or ownership.classify(child_rel) is ownership.Ownership.PAGE:
                        continue
                    stored[child.name] = Entry(
                        Path(child.path), child.is_dir(follow_symlinks=False), disk=child,
                    )
        except FileNotFoundError:
            if not stored:
                raise
        return sorted(stored.values(), key=lambda entry: entry.name)

    def _stored_entries(self, rel: str, limit: int | None) -> dict[str, Entry]:
        prefix = f"{rel}/" if rel else ""
        # Each recursive step seeks past the previous child's subtree through the primary key.
        # LIMIT bounds the names visited, even when one child contains the whole vault.
        # The final join uses the covering metadata index, keeping large body overflow off this read.
        sql = """
            WITH RECURSIVE children(path, tail) AS (
                SELECT path, substr(path, :start) FROM (
                    SELECT path FROM store_pages
                    WHERE path >= :prefix AND path < :upper ORDER BY path LIMIT 1
                )
                UNION ALL
                SELECT p.path, substr(p.path, :start) FROM children
                JOIN store_pages AS p ON p.path = (
                    SELECT min(path) FROM store_pages
                    WHERE path >= CASE WHEN instr(children.tail, '/') > 0
                        THEN :prefix || substr(children.tail, 1, instr(children.tail, '/') - 1) || '0'
                        ELSE children.path || char(0) END
                    AND path < :upper
                )
                LIMIT :limit
            )
            SELECT children.tail, p.path, p.revision, p.sha256, p.size, p.mtime_ns, p.frontmatter_json
            FROM children JOIN store_pages AS p INDEXED BY store_pages_meta ON p.path = children.path
        """
        found = {}
        with read.snapshot(self.root) as view:
            for tail, *row in view.connection.execute(sql, {
                "start": len(prefix) + 1, "prefix": prefix,
                "upper": prefix + read.AFTER, "limit": -1 if limit is None else limit,
            }):
                name, slash, _rest = tail.partition("/")
                found[name] = Entry(
                    self.root / prefix / name, bool(slash),
                    None if slash else read.PageMeta(*row),
                )
            view.memo.setdefault("page_metadata", {}).update(
                (entry.meta.path, entry.meta) for entry in found.values() if entry.meta is not None
            )
        return found

    def prefetch(self, entries: list[Entry], *, max_files: int, max_bytes: int) -> None:
        self._candidates = [
            entry for entry in entries
            if entry.meta is not None and entry.name.lower().endswith(".md")
        ]
        self._positions = {entry.meta.path: index for index, entry in enumerate(self._candidates)}
        self._batch_size = max(1, max_files)
        self._prefetch(self._candidates, max_files=max_files, max_bytes=max_bytes)

    def _prefetch(self, entries: list[Entry], *, max_files: int, max_bytes: int) -> None:
        wanted = []
        for entry in entries:
            if max_files <= 0:
                break
            if entry.meta.size <= max_bytes:
                wanted.append(entry.meta.path)
                max_bytes -= entry.meta.size
                max_files -= 1
        with read.snapshot(self.root) as view:
            view.prefetch_pages(wanted)

    def body(self, entry: Entry, *, max_files: int, max_bytes: int) -> bytes:
        with read.snapshot(self.root) as view:
            if entry.meta.path not in view.memo.get("pages", {}):
                # Undecodable pages consume no census slot. Grow the next batch so those pages
                # do not turn a bounded scan into one point query for every remaining page.
                self._batch_size *= 2
                self._prefetch(
                    self._candidates[self._positions[entry.meta.path]:],
                    max_files=max(max_files, self._batch_size), max_bytes=max_bytes,
                )
            return view.page(entry.meta.path).body
