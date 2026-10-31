"""The member manifest of a staged import: which files of `_Imports/<import_id>/` adoption may remove.

Two writers record the same list, one for each route that stages a folder: the Cloud import Job in
its receipt, and `exomem import` in a stage record when an import finishes. Both live in the vault's
state folder, outside the vault. A restore, a moved vault and a hand-made `_Imports/` folder have
none, so nothing in them is consumable.
"""
from __future__ import annotations

import dataclasses
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from . import private_state, state_migration, state_paths, vault

if TYPE_CHECKING:
    from . import held_fs

STAGE_DIR = vault.IMPORT_STAGE_DIRNAME
CLOUD_RECEIPTS = "cloud-imports"
LOCAL_RECORDS = "local-imports"
# What a run records as the reason its stage is trusted; the two values are a closed protocol pair.
CLOUD_PROOF = "cloud-receipt"
LOCAL_PROOF = "local-record"
MEMBERS_KEY = "members"
IDENTITY_KEY = "identity"


@dataclass(frozen=True, slots=True)
class Identity:
    """What a staged file's stat says about it, without the device number.

    Each pod can see one volume under another device number, so the device is never part of it.
    A backup restore gives a file another inode and cannot set its change time, which is how a
    mismatch shows that the file may hold other bytes. The fields are spelled here and nowhere else:
    `record`, `read` and the comparisons below all go through the dataclass.
    """

    inode: int
    size: int
    mtime_ns: int
    ctime_ns: int

    @classmethod
    def of(cls, info: os.stat_result) -> Identity:
        return cls(info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)

    @classmethod
    def of_version(cls, version: held_fs.FileVersion) -> Identity:
        """The same identity, read from a held file's version."""
        return cls(version.identity.inode, version.size, version.mtime_ns, version.change_time_ns)

    @classmethod
    def read(cls, value: object) -> Identity | None:
        """The identity a manifest entry records, or None when it is absent or malformed (hash the file then)."""
        if not isinstance(value, dict):
            return None
        fields = [value.get(item.name) for item in dataclasses.fields(cls)]
        if any(not isinstance(item, int) or isinstance(item, bool) or item < 0 for item in fields):
            return None
        return cls(*fields)

    def record(self) -> dict:
        return dataclasses.asdict(self)

    def after_rename(self) -> Identity:
        """What a rename leaves of the identity: the rename changes the file's change time, so that field is dropped."""
        return dataclasses.replace(self, ctime_ns=0)


def member(path: str, size: int, sha256: str, identity: Identity | None = None) -> dict:
    """One manifest entry: the file's path relative to `_Imports/<import_id>/`, its size and SHA-256.

    A Job that staged the file also records its `identity`, read after it set the file's time.
    """
    entry = {"path": path, "size": size, "sha256": sha256}
    if identity is not None:
        entry[IDENTITY_KEY] = identity.record()
    return entry


@dataclass(frozen=True, slots=True)
class Stage:
    """A stage with a manifest: `members` maps each listed path to its (size, SHA-256).

    `identities` holds the identity of each member that has a well-formed one; a member without
    one is hashed before adoption trusts it.
    """

    import_id: str
    proof: str
    members: dict[str, tuple[int, str]]
    identities: dict[str, Identity] = field(default_factory=dict)


def _read_members(entries: object) -> tuple[dict[str, tuple[int, str]], dict[str, Identity]] | None:
    """The manifest as maps, or None when it is malformed: a manifest that cannot be trusted lists nothing."""
    if not isinstance(entries, list):
        return None
    members: dict[str, tuple[int, str]] = {}
    identities: dict[str, Identity] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            return None
        path, size, digest = entry.get("path"), entry.get("size"), entry.get("sha256")
        if (not isinstance(path, str) or not isinstance(digest, str) or path in members
                or not isinstance(size, int) or isinstance(size, bool) or size < 0):
            return None
        members[path] = (size, digest)
        identity = Identity.read(entry.get(IDENTITY_KEY))
        if identity is not None:
            identities[path] = identity
    return members, identities


def _load(path: Path) -> dict | None:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return document if isinstance(document, dict) else None


def read(vault_root: Path, import_id: str) -> Stage | None:
    """The stage's manifest and what vouches for it, or None when it has none.

    Nothing here raises: a missing, unreadable or malformed manifest leaves the stage unrecorded,
    which only means adoption keeps its staged copies.
    """
    if not vault.is_import_id(import_id):
        return None
    try:
        state = state_paths.vault_state_dir(vault_root)
    except ValueError:
        return None
    receipt = _load(state / CLOUD_RECEIPTS / f"{import_id}.json")
    if receipt is not None and (receipt.get("request") or {}).get("kind") == "adopt":
        manifest = _read_members(receipt.get(MEMBERS_KEY))
        if manifest is not None:
            return Stage(import_id, CLOUD_PROOF, *manifest)
    record = _load(state / LOCAL_RECORDS / f"{import_id}.json")
    if record is not None:
        manifest = _read_members(record.get(MEMBERS_KEY))
        if manifest is not None:
            return Stage(import_id, LOCAL_PROOF, *manifest)
    return None


def write_local_record(vault_root: Path, import_id: str, members: list[dict]) -> bool:
    """Record a finished local import's manifest; False when the vault's state placement is not settled.

    A state folder made before the vault's first start would make that start refuse with a
    placement conflict, so an unsettled vault gets no record and adoption keeps the staged copy.
    """
    try:
        if state_migration.migration_status(vault_root) != "complete":
            return False
        store = private_state.store(vault_root, LOCAL_RECORDS)
        private_state.write_record(
            store, import_id, {"import_id": import_id, MEMBERS_KEY: members}, temp_suffix=".tmp")
    except (OSError, ValueError):
        return False
    return True
