"""Removal of a staged artifact once the Knowledge Base holds its identical copy.

Adoption removes a staged file only when a stage manifest (`import_stage`) lists it, the staged
bytes still match that entry and the plan, and the durable copy matches the plan. The staged file
is held while it is hashed and unlinked, so the file that goes is the file that was hashed. Every
refusal keeps the staged copy: a copy that stays costs disk, and a copy that goes wrongly costs data.
"""
from __future__ import annotations

import hashlib
import os
from enum import Enum
from pathlib import Path

from . import held_fs, import_stage


class Removal(Enum):
    """What happened to one staged copy: `MISSING` means it was already gone, `KEPT` that it stays."""

    REMOVED = "removed"
    MISSING = "missing"
    KEPT = "kept"


def recorded(root: Path, run: dict) -> import_stage.Stage | None:
    """The manifest of the stage this run started in, as it is now; None when the run has no recorded stage.

    The manifest is read from the state folder each time it is needed, never copied into the run, so a
    discarded stage stops being consumable at once.
    """
    recorded_stage = run.get("stage")
    if not isinstance(recorded_stage, dict):
        return None
    return import_stage.read(root, recorded_stage.get("stage", ""))


def entry_of(stage: import_stage.Stage, original_path: str) -> tuple[int, str] | None:
    """The manifest's (size, SHA-256) for a staged file given by its vault-relative path, or None if unlisted."""
    prefix = f"{import_stage.STAGE_DIR}/{stage.import_id}/"
    if not original_path.startswith(prefix):
        return None
    return stage.members.get(original_path[len(prefix):])


def _sha256_of(file: held_fs.HeldFile) -> str:
    with os.fdopen(os.dup(file.descriptor), "rb") as stream:
        stream.seek(0)
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _matches(file: held_fs.HeldFile, size: int, sha256: str) -> bool:
    """Whether the held file has these bytes, and was not written while it was read."""
    version = file.version()
    return version.size == size and _sha256_of(file) == sha256 and file.version() == version


def durable_matches(root: Path, artifact_path: str | None, planned: dict) -> bool:
    """Whether the Knowledge Base artifact has the planned bytes, read back in a stream."""
    if not artifact_path:
        return False
    parent, _, leaf = artifact_path.rpartition("/")
    try:
        with held_fs.acquire(root).require() as filesystem:
            with filesystem.parent(parent or ".").require() as directory:
                with filesystem.file(directory, leaf).require() as durable:
                    return _matches(durable, planned["original_bytes"], planned["original_sha256"])
    except (held_fs.HeldFsError, OSError):
        return False


def remove_staged(root: Path, stage: import_stage.Stage, planned: dict) -> Removal:
    """Unlink the item's staged copy when it is the file the manifest and the plan describe.

    The caller has already shown that the durable copy matches. A copy that does not match, is
    unlisted, or cannot be unlinked stays and the item stays applied.
    """
    entry = entry_of(stage, planned["original_path"])
    if entry != (planned["original_bytes"], planned["original_sha256"]):
        return Removal.KEPT
    parent, _, leaf = planned["original_path"].rpartition("/")
    try:
        with held_fs.acquire(root).require() as filesystem:
            found = filesystem.parent(parent, access="mutate")
            if not found.ok:
                return Removal.MISSING if found.error.code == "MISSING" else Removal.KEPT
            with found.require() as directory:
                opened = filesystem.file(directory, leaf, access="mutate")
                if not opened.ok:
                    return Removal.MISSING if opened.error.code == "MISSING" else Removal.KEPT
                with opened.require() as staged:
                    if not _matches(staged, *entry):
                        return Removal.KEPT
                    filesystem.unlink(staged).require()
    except (held_fs.HeldFsError, OSError):
        return Removal.KEPT
    return Removal.REMOVED
