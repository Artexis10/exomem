"""The `list_trash` Tier 2 op: enumerate recoverable trash entries.

Walks `Knowledge Base/_trash/YYYY-MM-DD/` dirs, parses each `.meta.json`
sidecar, and returns a structured list. Without this, callers have to
`list_directory` the trash and walk sidecars manually — the trash is
technically reachable but not ergonomic.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

from .vault import kb_root


log = logging.getLogger(__name__)

TRASH_SUBPATH = "_trash"


@dataclass
class TrashEntry:
    trash_path: str       # vault-relative POSIX, points at the trashed file/dir
    meta_path: str        # vault-relative POSIX of the .meta.json sidecar
    original_path: str    # where it lived before being trashed
    trashed_at: str       # ISO datetime
    kind: str             # "file" | "directory"
    file_count: int | None
    inbound_link_count_at_trash: int
    force_orphan_used: bool
    force_superseded_used: bool
    allow_curated_used: bool

    def as_dict(self) -> dict:
        return {
            "trash_path": self.trash_path,
            "meta_path": self.meta_path,
            "original_path": self.original_path,
            "trashed_at": self.trashed_at,
            "kind": self.kind,
            "file_count": self.file_count,
            "inbound_link_count_at_trash": self.inbound_link_count_at_trash,
            "force_orphan_used": self.force_orphan_used,
            "force_superseded_used": self.force_superseded_used,
            "allow_curated_used": self.allow_curated_used,
        }


@dataclass
class ListTrashResult:
    entries: list[TrashEntry]
    count: int
    orphan_sidecars: list[str]  # sidecars whose target file is missing
    orphan_files: list[str]     # trash files without a sidecar

    def as_dict(self) -> dict:
        return {
            "entries": [e.as_dict() for e in self.entries],
            "count": self.count,
            "orphan_sidecars": self.orphan_sidecars,
            "orphan_files": self.orphan_files,
        }


def list_trash(
    vault_root: Path, *, date: str | None = None
) -> ListTrashResult:
    """List trash entries, most recent first.

    Args:
        date: Optional YYYY-MM-DD filter. If None, returns all dates.

    Returns: {entries, count, orphan_sidecars, orphan_files}. Orphans are
    drift hints — sidecars without files (means someone moved/deleted the
    file without cleaning the sidecar) or files without sidecars (means
    the trash was written before sidecars were a thing, or the meta write
    failed).
    """
    trash_root = kb_root(vault_root) / TRASH_SUBPATH
    if not trash_root.is_dir():
        return ListTrashResult(entries=[], count=0, orphan_sidecars=[], orphan_files=[])

    entries: list[TrashEntry] = []
    orphan_sidecars: list[str] = []
    orphan_files: list[str] = []

    date_dirs = sorted(
        (d for d in trash_root.iterdir() if d.is_dir()),
        key=lambda p: p.name,
        reverse=True,
    )

    for date_dir in date_dirs:
        if date and date_dir.name != date:
            continue
        try:
            children = list(date_dir.iterdir())
        except OSError:
            continue

        sidecars = {c for c in children if c.is_file() and c.name.endswith(".meta.json")}
        non_sidecars = [c for c in children if c not in sidecars]

        for sidecar in sidecars:
            # Sidecar name: <trash_name>.meta.json
            target_name = sidecar.name[: -len(".meta.json")]
            target = date_dir / target_name
            try:
                meta = json.loads(sidecar.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                meta = {}
            if not target.exists():
                try:
                    sidecar_rel = sidecar.resolve().relative_to(vault_root.resolve()).as_posix()
                except (ValueError, OSError):
                    sidecar_rel = sidecar.as_posix()
                orphan_sidecars.append(sidecar_rel)
                continue
            try:
                trash_rel = target.resolve().relative_to(vault_root.resolve()).as_posix()
                meta_rel = sidecar.resolve().relative_to(vault_root.resolve()).as_posix()
            except (ValueError, OSError):
                continue
            entries.append(TrashEntry(
                trash_path=trash_rel,
                meta_path=meta_rel,
                original_path=str(meta.get("original_path", "")),
                trashed_at=str(meta.get("trashed_at", "")),
                kind="directory" if target.is_dir() else "file",
                file_count=meta.get("file_count_at_trash"),
                inbound_link_count_at_trash=int(
                    meta.get("inbound_link_count_at_trash", 0) or 0
                ),
                force_orphan_used=bool(meta.get("force_orphan_used", False)),
                force_superseded_used=bool(meta.get("force_superseded_used", False)),
                allow_curated_used=bool(meta.get("allow_curated_used", False)),
            ))

        for nonsc in non_sidecars:
            expected = date_dir / f"{nonsc.name}.meta.json"
            if expected not in sidecars:
                try:
                    rel = nonsc.resolve().relative_to(vault_root.resolve()).as_posix()
                except (ValueError, OSError):
                    rel = nonsc.as_posix()
                orphan_files.append(rel)

    # Sort entries most-recent-first by trashed_at when available, else by trash_path.
    entries.sort(
        key=lambda e: (e.trashed_at or "", e.trash_path),
        reverse=True,
    )

    return ListTrashResult(
        entries=entries,
        count=len(entries),
        orphan_sidecars=orphan_sidecars,
        orphan_files=orphan_files,
    )
