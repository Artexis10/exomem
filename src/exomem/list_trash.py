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

from . import reserved_paths
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
    trash_root_rel = trash_root.relative_to(vault_root).as_posix()
    try:
        root_identity = reserved_paths.inspect_generic_path(
            vault_root, trash_root_rel
        )
    except reserved_paths.ReservedPathLeafError:
        return ListTrashResult(entries=[], count=0, orphan_sidecars=[], orphan_files=[])
    if root_identity.kind != "directory":
        return ListTrashResult(entries=[], count=0, orphan_sidecars=[], orphan_files=[])

    entries: list[TrashEntry] = []
    orphan_sidecars: list[str] = []
    orphan_files: list[str] = []

    try:
        root_entries = reserved_paths.list_generic_tree(
            vault_root,
            trash_root_rel,
            recursive=False,
        )
    except reserved_paths.ReservedPathLeafError:
        root_entries = ()
    date_names = sorted(
        (
            entry.relative_path
            for entry in root_entries
            if entry.identity.kind == "directory"
        ),
        reverse=True,
    )

    for date_name in date_names:
        if date and date_name != date:
            continue
        date_rel = f"{trash_root_rel}/{date_name}"
        try:
            children = reserved_paths.list_generic_tree(
                vault_root,
                date_rel,
                recursive=False,
            )
        except reserved_paths.ReservedPathLeafError:
            continue

        by_name = {child.relative_path: child for child in children}
        sidecar_names = {
            child.relative_path
            for child in children
            if child.identity.kind == "file"
            and child.relative_path.endswith(".meta.json")
        }
        non_sidecars = [
            child for child in children if child.relative_path not in sidecar_names
        ]

        for sidecar_name in sorted(sidecar_names):
            # Sidecar name: <trash_name>.meta.json
            target_name = sidecar_name[: -len(".meta.json")]
            sidecar_rel = f"{date_rel}/{sidecar_name}"
            try:
                sidecar = reserved_paths.read_generic_bytes(vault_root, sidecar_rel)
                meta = json.loads(sidecar.data.decode("utf-8"))
            except (
                OSError,
                UnicodeDecodeError,
                json.JSONDecodeError,
                reserved_paths.ReservedPathLeafError,
            ):
                meta = {}

            target = by_name.get(target_name)
            if target is None:
                orphan_sidecars.append(sidecar_rel)
                continue
            target_rel = f"{date_rel}/{target_name}"
            try:
                current_target = reserved_paths.inspect_generic_path(
                    vault_root, target_rel
                )
            except reserved_paths.ReservedPathLeafError:
                orphan_sidecars.append(sidecar_rel)
                continue
            if current_target != target.identity:
                orphan_sidecars.append(sidecar_rel)
                continue
            entries.append(TrashEntry(
                trash_path=target_rel,
                meta_path=sidecar_rel,
                original_path=str(meta.get("original_path", "")),
                trashed_at=str(meta.get("trashed_at", "")),
                kind=current_target.kind,
                file_count=meta.get("file_count_at_trash"),
                inbound_link_count_at_trash=int(
                    meta.get("inbound_link_count_at_trash", 0) or 0
                ),
                force_orphan_used=bool(meta.get("force_orphan_used", False)),
                force_superseded_used=bool(meta.get("force_superseded_used", False)),
                allow_curated_used=bool(meta.get("allow_curated_used", False)),
            ))

        for nonsc in non_sidecars:
            if f"{nonsc.relative_path}.meta.json" not in sidecar_names:
                orphan_files.append(f"{date_rel}/{nonsc.relative_path}")

    # Sort entries most-recent-first by trashed_at when available, else by trash_path.
    entries.sort(
        key=lambda e: (e.trashed_at or "", e.trash_path),
        reverse=True,
    )

    return ListTrashResult(
        entries=entries,
        count=len(entries),
        orphan_sidecars=sorted(orphan_sidecars),
        orphan_files=sorted(orphan_files),
    )
