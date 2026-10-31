"""Filing an artifact member of a recorded stage by renaming it into the Knowledge Base (design D8, D12).

The stage and the Knowledge Base share a filesystem, so a rename files a member with no copy. The
manifest identity proves that the staged file still holds the manifest's bytes without reading them.
The rename never replaces a file. It happens before the Source page commits, and the page is the
binary's sidecar, so the batch writer never lists the binary and its rollback cannot remove it.

A stop leaves the binary renamed and the page uncommitted. `settle` finishes that state on the next
`apply`; it never moves the file back into a stage, because a discard deletes the stage. `put_back`
undoes a rename only for a batch that failed in the same call.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from . import adoption_consume, adoption_plan, held_fs, import_stage, rename_journal

#: The outcome code of a file that stays at its Knowledge Base path with no Source page.
UNBOUND = "ARTIFACT_UNBOUND"


@dataclass(frozen=True, slots=True)
class Placed:
    """A member now at its Knowledge Base path, with its Source page still to commit."""

    original_path: str
    artifact_path: str
    inode: int
    #: Found there by a later call, not renamed by this one; such a file never goes back to a stage.
    replayed: bool = False


@dataclass(frozen=True, slots=True)
class Refused:
    """A member that stays where it is, with the outcome code and reason that report it."""

    code: str
    reason: str


def _split(path: str) -> tuple[str, str]:
    parent, _, leaf = path.rpartition("/")
    return parent, leaf


def renamable(stage: import_stage.Stage | None, planned: dict) -> bool:
    """Whether a member files by rename: an artifact that a recorded stage lists with the plan's size and SHA-256."""
    return (
        stage is not None and planned["form"] == "artifact"
        and adoption_consume.entry_of(stage, planned["original_path"])
        == (planned["original_bytes"], planned["original_sha256"])
    )


def _same(version: held_fs.FileVersion, identity: import_stage.Identity, *, with_ctime: bool) -> bool:
    seen = import_stage.Identity.of_version(version)
    return seen == identity if with_ctime else seen.after_rename() == identity.after_rename()


def written_bytes(root: Path, stage: import_stage.Stage | None, planned: dict) -> int:
    """The bytes that filing this member writes to the vault's volume: none when a rename files it on one device.

    A member whose staged file is gone is already on the volume (an earlier call renamed it, or it
    never was there), so it adds nothing either.
    """
    if not renamable(stage, planned):
        return planned["original_bytes"]
    try:
        staged_device = os.lstat(root / planned["original_path"]).st_dev
    except FileNotFoundError:
        return 0
    except OSError:
        return planned["original_bytes"]
    return 0 if staged_device == _device_of(root, planned["artifact_path"]) else planned["original_bytes"]


def _device_of(root: Path, relative_path: str) -> int | None:
    """The device of a vault-relative path, or of its nearest folder that exists (a rename creates the rest)."""
    path = root / relative_path
    for candidate in (path, *path.parents):
        try:
            return os.lstat(candidate).st_dev
        except OSError:
            continue
    return None


def _hash_within(
    file: held_fs.HeldFile, planned: dict, ceiling: int, *, where: str
) -> Refused | None:
    """Hash the file once between two stat checks, within the ceiling; a Refused when it does not match."""
    if file.version().size > ceiling:
        return Refused(
            adoption_plan.OVER_CEILING,
            f"{where} no longer matches its manifest identity and is above the {ceiling:,} byte adoption ceiling",
        )
    if not adoption_consume.matches(file, planned["original_bytes"], planned["original_sha256"]):
        return Refused("SOURCE_CHANGED", f"{where} differs from the staged manifest")
    return None


def _prove_staged(file: held_fs.HeldFile, stage: import_stage.Stage, planned: dict, ceiling: int) -> Refused | None:
    identity = adoption_consume.identity_of(stage, planned["original_path"])
    if identity is not None and _same(file.version(), identity, with_ctime=True):
        return None  # the manifest SHA-256 stands for the bytes; none is read
    return _hash_within(file, planned, ceiling, where="the staged file")


def place(
    root: Path, filesystem: held_fs.HeldFilesystem, stage: import_stage.Stage, planned: dict, *,
    ceiling: int, inode: int,
) -> Placed | Refused | None:
    """Rename the member's staged file to its Knowledge Base path; None when the copy route must file it.

    `inode` is the one the journal recorded for this member before the rename. The record is the caller's:
    this function neither writes nor releases it. A rename that fails across devices returns None.
    """
    staged_parent, staged_leaf = _split(planned["original_path"])
    artifact_parent, artifact_leaf = _split(planned["artifact_path"])
    try:
        with filesystem.parent(staged_parent, access="mutate").require() as source_directory:
            with filesystem.file(source_directory, staged_leaf, access="mutate").require() as staged:
                refusal = _prove_staged(staged, stage, planned, ceiling)
                if refusal is not None:
                    return refusal
                if staged.version().identity.inode != inode:
                    return Refused("SOURCE_CHANGED", "the staged file was replaced while it was being filed")
                with filesystem.parent(artifact_parent, create=True, access="mutate").require() as target_directory:
                    moved = filesystem.rename(staged, target_directory, artifact_leaf)
                if moved.ok or _is_at_target(filesystem, artifact_parent, artifact_leaf, inode):
                    # An error after the rename took place (a failed sync) still leaves the file there.
                    return Placed(planned["original_path"], planned["artifact_path"], inode)
                code = moved.error.code  # held_fs's closed error codes, so a comparison is sound here
                if code == "CROSS_DEVICE":
                    return None
                if code == "DESTINATION_EXISTS":
                    return Refused("TARGET_OCCUPIED", f"{planned['artifact_path']} already exists and is not this file")
                return Refused(**adoption_plan.source_failure(moved.error))
    except (held_fs.HeldFsError, OSError) as error:
        if getattr(error, "code", None) == "MISSING":
            return None  # nothing staged to rename; the copy route reports it as it always did
        return Refused(**adoption_plan.source_failure(error))


def _is_at_target(filesystem: held_fs.HeldFilesystem, parent: str, leaf: str, inode: int) -> bool:
    try:
        with filesystem.parent(parent).require() as directory:
            with filesystem.file(directory, leaf).require() as found:
                return found.version().identity.inode == inode
    except (held_fs.HeldFsError, OSError):
        return False


def place_all(
    root: Path, stage: import_stage.Stage, members: list[dict], *, ceiling: int, run_id: str
) -> dict[str, Placed | Refused]:
    """`place` for each member, by the member's staged path, after one flushed journal write for all of them.

    A filesystem that cannot be held, or a journal that cannot be written, files by copy. A member that
    is not renamed keeps no record.
    """
    holds: dict[str, rename_journal.Hold] = {}
    for planned in members:
        try:
            inode = os.lstat(root / planned["original_path"]).st_ino
        except OSError:
            continue  # nothing staged to rename; the copy route reports it
        holds[planned["original_path"]] = rename_journal.Hold(
            planned["artifact_path"], inode, stage.import_id, run_id, planned["original_path"])
    verdicts: dict[str, Placed | Refused | None] = {}
    try:
        rename_journal.hold_all(root, list(holds.values()))
        with held_fs.acquire(root).require() as filesystem:
            for planned in members:
                hold = holds.get(planned["original_path"])
                if hold is not None:
                    verdicts[planned["original_path"]] = place(
                        root, filesystem, stage, planned, ceiling=ceiling, inode=hold.inode)
    except (held_fs.HeldFsError, OSError):
        pass
    for original_path, hold in holds.items():
        if not isinstance(verdicts.get(original_path), Placed):
            rename_journal.release(root, hold.path)
    return {path: verdict for path, verdict in verdicts.items() if isinstance(verdict, Placed | Refused)}


def settle(root: Path, stage: import_stage.Stage, planned: dict, *, ceiling: int) -> Placed | Refused | None:
    """Replay one member whose staged file is gone and whose Source page is not committed.

    The binary at the Knowledge Base path is the member when its inode, size and mtime match the
    manifest identity (the rename changed the ctime, so that is not compared). Otherwise it is hashed
    where it is, within the ceiling. A file that cannot be vouched for stays unbound at its path.
    Returns None when no such binary is there.
    """
    if not renamable(stage, planned) or os.path.lexists(root / planned["target_path"]):
        return None
    artifact_parent, artifact_leaf = _split(planned["artifact_path"])
    try:
        with held_fs.acquire(root).require() as filesystem:
            with filesystem.parent(artifact_parent).require() as directory:
                with filesystem.file(directory, artifact_leaf).require() as binary:
                    inode = binary.version().identity.inode
                    identity = adoption_consume.identity_of(stage, planned["original_path"])
                    if identity is None or not _same(binary.version(), identity, with_ctime=False):
                        refusal = _hash_within(binary, planned, ceiling, where=f"the file at {planned['artifact_path']}")
                        if refusal is not None:
                            return Refused(
                                refusal.code,
                                f"{refusal.reason}; it stays unbound at {planned['artifact_path']}",
                            )
    except (held_fs.HeldFsError, OSError):
        return None
    return Placed(planned["original_path"], planned["artifact_path"], inode, replayed=True)


def put_back(root: Path, placed: Placed) -> Refused | None:
    """Rename a binary of a batch that failed back to its staged path; None when it is back.

    The rename never replaces a file, and it recreates a staged folder that went. The binary is the
    only copy, so a failure of any kind leaves it where it is and reports it as unbound.
    """
    artifact_parent, artifact_leaf = _split(placed.artifact_path)
    staged_parent, staged_leaf = _split(placed.original_path)
    stays = Refused(UNBOUND, f"the file stays at {placed.artifact_path}: its Source page did not commit")
    try:
        with held_fs.acquire(root).require() as filesystem:
            with filesystem.parent(artifact_parent, access="mutate").require() as directory:
                with filesystem.file(directory, artifact_leaf, access="mutate").require() as binary:
                    if binary.version().identity.inode != placed.inode:
                        return stays
                    with filesystem.parent(staged_parent, create=True, access="mutate").require() as stage_directory:
                        moved = filesystem.rename(binary, stage_directory, staged_leaf)
        if moved.ok:
            rename_journal.release(root, placed.artifact_path)
            return None
    except (held_fs.HeldFsError, OSError):
        pass
    return Refused(UNBOUND, f"{stays.reason} and its staged path {placed.original_path} is taken or unavailable")
