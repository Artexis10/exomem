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
# What a discard leaves in the receipt, in the same write that drops the manifest (see `residual_record`).
RESIDUAL_KEY = "residual"
RESIDUAL_VERSION = 1
# The renamed members that a refused discard found unsettled: their Knowledge Base paths, for the status to read.
# A readable receipt carries them; when it is unreadable, the walk file does (see `read_walk`).
RENAMES_PENDING_KEY = "renames_pending"
# Where a discard that walked the stage writes its record, when the receipt could not be read.
WALK_RECORDS = "cloud-import-walks"
# The code of a walked member: the receipt that would have said why it stayed was unreadable.
UNREADABLE_CODE = "receipt_unreadable"
# The file at a window's stage root that lists the whole series; it is never a member to file.
SERIES_FILE = ".exomem-series.json"


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


def receipt_path(vault_root: Path, import_id: str) -> Path:
    """Where the Cloud import Job keeps the import's receipt; raises ValueError when the vault has no state placement."""
    return state_paths.vault_state_dir(vault_root) / CLOUD_RECEIPTS / f"{import_id}.json"


def read_receipt(vault_root: Path, import_id: str) -> dict | None:
    """The import's receipt, or None when it is missing, unreadable or holds no request (so it is not a receipt)."""
    try:
        receipt = _load(receipt_path(vault_root, import_id))
    except ValueError:
        return None
    return receipt if receipt is not None and isinstance(receipt.get("request"), dict) else None


def read(vault_root: Path, import_id: str) -> Stage | None:
    """The stage's manifest and what vouches for it, or None when it has none.

    Nothing here raises: a missing, unreadable or malformed manifest leaves the stage unrecorded,
    which only means adoption keeps its staged copies.
    """
    if not vault.is_import_id(import_id):
        return None
    receipt = read_receipt(vault_root, import_id)
    if receipt is not None and receipt["request"].get("kind") == "adopt":
        manifest = _read_members(receipt.get(MEMBERS_KEY))
        if manifest is not None:
            return Stage(import_id, CLOUD_PROOF, *manifest)
    try:
        record = _load(state_paths.vault_state_dir(vault_root) / LOCAL_RECORDS / f"{import_id}.json")
    except ValueError:
        return None
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


def staged_files(vault_root: Path, import_id: str) -> list[tuple[str, int]]:
    """Each file under the stage as (path relative to the stage, bytes); a file whose size cannot be read is left out."""
    base = vault_root / STAGE_DIR / import_id
    found: list[tuple[str, int]] = []
    for directory, _names, files in os.walk(base):
        for name in files:
            full = Path(directory) / name
            try:
                found.append((full.relative_to(base).as_posix(), full.lstat().st_size))
            except OSError:
                continue
    return found


def residual_member(path: str, size: int, code: str) -> dict:
    """One member that adoption did not file: its path relative to the stage, its bytes and why it stayed."""
    return {"path": path, "bytes": size, "code": code}


def residual_record(members: list[dict]) -> dict:
    """The residual record of a discarded window, one entry per member that adoption did not file.

    An empty list says that every member was filed. A member is `{"path", "bytes", "code"}` with the path
    relative to the stage, as the manifest gives it. The record is written in the receipt (`RESIDUAL_KEY`),
    or, when the receipt could not be read, in the walk store (`WALK_RECORDS`). `read_residual` reads both.
    """
    return {"version": RESIDUAL_VERSION, MEMBERS_KEY: members}


def renames_pending_record(paths: list[str]) -> dict:
    """The walk file of a walking discard that renamed members refused: their Knowledge Base paths and no member list.

    It has no `members` key, so `read_residual` finds no record in it and a later walk still writes its own.
    """
    return {"version": RESIDUAL_VERSION, RENAMES_PENDING_KEY: paths}


def read_walk(vault_root: Path, import_id: str) -> dict | None:
    """The walk file of an import whose receipt could not be read, or None when it has none or is damaged.

    The file holds the `members` of a stage walk (see `residual_record`), or only the `renames_pending` of a
    walking discard that a renamed member refused; the two are told apart by the key.
    """
    if not vault.is_import_id(import_id):
        return None
    try:
        return _load(state_paths.vault_state_dir(vault_root) / WALK_RECORDS / f"{import_id}.json")
    except ValueError:
        return None


def _members_of(record: object) -> list[dict] | None:
    """The member list of a residual record, or None for anything else: another version, a damaged list, no list."""
    entries = record.get(MEMBERS_KEY) if isinstance(record, dict) and record.get("version") == RESIDUAL_VERSION else None
    return entries if isinstance(entries, list) and all(isinstance(entry, dict) for entry in entries) else None


def read_walk_members(vault_root: Path, import_id: str) -> list[dict] | None:
    """The members that a stage walk recorded, or None when no walk did (a pending-only walk file records none)."""
    return _members_of(read_walk(vault_root, import_id))


def read_residual(vault_root: Path, import_id: str) -> list[dict] | None:
    """The members of a discarded window's residual record, or None when the window has none.

    The receipt's record wins; a stage walk's record stands in for a receipt that could not be read.
    A record of another version, or a damaged one, is none: the caller never reads a wrong list.
    """
    if not vault.is_import_id(import_id):
        return None
    receipt_members = _members_of((read_receipt(vault_root, import_id) or {}).get(RESIDUAL_KEY))
    return receipt_members if receipt_members is not None else read_walk_members(vault_root, import_id)


def _stems(directory: Path) -> set[str]:
    """The import IDs that name a record file in `directory`; a missing folder holds none, an unreadable one raises."""
    try:
        with os.scandir(directory) as entries:
            return {Path(entry.name).stem for entry in entries if entry.name.endswith(".json") and vault.is_import_id(Path(entry.name).stem)}
    except FileNotFoundError:
        return set()


def window_ids(vault_root: Path) -> list[str]:
    """The import IDs of every window with a Cloud receipt or a walk file, in order; raises OSError when a folder cannot be listed."""
    try:
        state = state_paths.vault_state_dir(vault_root)
    except ValueError:
        return []  # A vault with no state placement has no receipt and no walk file, as `read_receipt` finds.
    return sorted(_stems(state / CLOUD_RECEIPTS) | _stems(state / WALK_RECORDS))


def clear_residual(vault_root: Path, import_id: str, paths: set[str]) -> list[str] | None:
    """Drop the named members from the window's residual record, and return the paths it dropped.

    None means the window has no record to clear. The record is written where `read_residual` reads it,
    so the receipt's record outranks a walk's. A path that the record does not list changes nothing.
    """
    receipt = read_receipt(vault_root, import_id)
    record = receipt.get(RESIDUAL_KEY) if receipt is not None else None
    if _members_of(record) is not None:
        assert receipt is not None
        dropped = _drop(record, paths)
        if dropped:
            _store_record(vault_root, CLOUD_RECEIPTS, import_id, receipt)
        return dropped
    walk = read_walk(vault_root, import_id)
    if _members_of(walk) is None:
        return None
    assert walk is not None
    dropped = _drop(walk, paths)
    if dropped:
        _store_record(vault_root, WALK_RECORDS, import_id, walk)
    return dropped


def _drop(record: dict, paths: set[str]) -> list[str]:
    """Remove the members whose path is in `paths` from `record`, in place; return the paths it removed."""
    kept = [entry for entry in record[MEMBERS_KEY] if entry.get("path") not in paths]
    dropped = [entry["path"] for entry in record[MEMBERS_KEY] if entry.get("path") in paths]
    record[MEMBERS_KEY] = kept
    return dropped


def _store_record(vault_root: Path, dirname: str, import_id: str, record: dict) -> None:
    """Replace one record whole and durably, as the Job writes it."""
    store = private_state.store(vault_root, dirname)
    private_state.write_record(store, import_id, record, temp_suffix=".tmp", durable=True)
    private_state.sync_directory(store)
