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
# Members that the person cleared from a residual record (D12): the same entry plus the time, kept beside `members`.
ACKNOWLEDGED_KEY = "acknowledged"
# What `window_record` finds, and which file holds a residual record; the status protocol's words.
WINDOW_LANDED, WINDOW_RESIDUAL, WINDOW_UNREADABLE = "landed", "residual", "unreadable"
RECEIPT_SOURCE, WALK_SOURCE = "receipt", "walk"


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


@dataclass(frozen=True, slots=True)
class Series:
    """What a window's series file says about its whole series: the label, and every member's path relative to a stage.

    The file is a JSON object `{"label": ..., "paths": [...]}`. A series file that omits either, or holds the
    wrong type for it, says nothing of it: `label` is None, and `paths` is empty. The paths are names only:
    adoption compares them as strings and never opens one.
    """

    label: str | None
    paths: tuple[str, ...]


def is_series_file(path: str) -> bool:
    """Whether a vault-relative path is the series file at the root of an import stage."""
    parts = path.split("/")
    return len(parts) == 3 and parts[0] == STAGE_DIR and parts[2] == SERIES_FILE


def read_series(vault_root: Path, import_id: str) -> Series | None:
    """The series that a window's staged series file records, or None when the stage holds no readable one."""
    if not vault.is_import_id(import_id):
        return None
    path = vault_root / STAGE_DIR / import_id / SERIES_FILE
    document = None if path.is_symlink() else _load(path)
    if document is None:
        return None
    label, paths = document.get("label"), document.get("paths")
    return Series(
        label.strip() if isinstance(label, str) and label.strip() else None,
        tuple(paths) if isinstance(paths, list) and all(isinstance(path, str) for path in paths) else (),
    )


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


def _read_json(path: Path) -> tuple[dict | None, bool]:
    """The JSON object in `path`, and whether the file exists but cannot be used (unreadable, not JSON, not an object)."""
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, False
    except (OSError, ValueError):
        return None, True
    return (document, False) if isinstance(document, dict) else (None, True)


def _load(path: Path) -> dict | None:
    return _read_json(path)[0]


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


def _cloud_stage(import_id: str, receipt: dict) -> Stage | None:
    """The stage that an `adopt` receipt's manifest vouches for, or None when it has no trustworthy manifest."""
    if receipt["request"].get("kind") != "adopt":
        return None
    manifest = _read_members(receipt.get(MEMBERS_KEY))
    return Stage(import_id, CLOUD_PROOF, *manifest) if manifest is not None else None


def read(vault_root: Path, import_id: str) -> Stage | None:
    """The stage's manifest and what vouches for it, or None when it has none.

    Nothing here raises: a missing, unreadable or malformed manifest leaves the stage unrecorded,
    which only means adoption keeps its staged copies.
    """
    if not vault.is_import_id(import_id):
        return None
    receipt = read_receipt(vault_root, import_id)
    if receipt is not None and (stage := _cloud_stage(import_id, receipt)) is not None:
        return stage
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
    """The member list of a record that holds one, or None: another version, no list, or entries that are not objects.

    This answers whether a walk file holds the members of a walk, which the Job asks. A reader asks `_record_of`.
    """
    entries = record.get(MEMBERS_KEY) if isinstance(record, dict) and record.get("version") == RESIDUAL_VERSION else None
    return entries if isinstance(entries, list) and all(isinstance(entry, dict) for entry in entries) else None


def read_walk_members(vault_root: Path, import_id: str) -> list[dict] | None:
    """The members that a stage walk recorded, or None when no walk did (a pending-only walk file records none)."""
    return _members_of(read_walk(vault_root, import_id))


def _well_formed(entries: list[dict], *, timed: bool) -> bool:
    """Whether every entry of a residual list is `{path, bytes, code}`, and has a time when `timed`."""
    return all(
        isinstance(entry.get("path"), str) and type(entry.get("bytes")) is int and entry["bytes"] >= 0
        and isinstance(entry.get("code"), str) and (not timed or isinstance(entry.get("at"), str))
        for entry in entries
    )


def _record_of(document: dict | None) -> tuple[list[dict], list[dict]] | None:
    """The unfiled and the acknowledged members of a residual record, or None when the record is damaged.

    Damaged is another version, an entry that is not `{path, bytes, code}`, or a path that the record lists
    twice, because a listing would then sort or page one member into two.
    """
    members = _members_of(document)
    if members is None or document is None:
        return None
    acknowledged = document.get(ACKNOWLEDGED_KEY, [])
    if not (isinstance(acknowledged, list) and all(isinstance(entry, dict) for entry in acknowledged)):
        return None
    if not (_well_formed(members, timed=False) and _well_formed(acknowledged, timed=True)):
        return None
    paths = [entry["path"] for entry in members + acknowledged]
    return (members, acknowledged) if len(set(paths)) == len(paths) else None


def _paths_of(value: object) -> tuple[str, ...]:
    """The strings of a list; a pending list is a hint for the status, so a damaged one reports none."""
    return tuple(value) if isinstance(value, list) and all(isinstance(item, str) for item in value) else ()


@dataclass(frozen=True, slots=True)
class WindowRecord:
    """What the cell holds for one window, as `window_record` decides it; the three kinds are the status protocol's words.

    `landed` has a stage with a manifest. `residual` has the unfiled and acknowledged members of a discarded
    window, read from `source`. `unreadable` has a record that cannot be used, or a stage and no record, so
    nothing about its members is known; `staged` says whether the stage folder still holds files.
    """

    kind: str
    stage: Stage | None = None
    members: tuple[dict, ...] = ()
    acknowledged: tuple[dict, ...] = ()
    source: str | None = None
    problem: str | None = None
    staged: bool = False
    renames_pending: tuple[str, ...] = ()


def window_record(vault_root: Path, import_id: str) -> WindowRecord | None:
    """The one record that stands for a window, or None when it has nothing left to report.

    The status, the acknowledgement and `read_residual` all read a window through here, so the order
    lives once. A residual record in the receipt comes first, because a discard writes it in the same write
    that drops the manifest. A stage walk's record with members comes next: it stands in for a receipt that
    could not be read, and it outranks a manifest that is readable again, because the walk found files that
    the manifest did not list. Then a manifest makes the window landed. A record that exists and cannot
    be used makes it unreadable, as does a stage with no record at all.
    """
    if not vault.is_import_id(import_id):
        return None
    try:
        receipt, receipt_damaged = _read_json(receipt_path(vault_root, import_id))
        walk, walk_damaged = _read_json(state_paths.vault_state_dir(vault_root) / WALK_RECORDS / f"{import_id}.json")
    except ValueError:
        return None  # A vault with no state placement has no receipt and no walk file.
    if receipt is not None and not isinstance(receipt.get("request"), dict):
        receipt, receipt_damaged = None, True
    pending = _paths_of((receipt if receipt is not None else walk or {}).get(RENAMES_PENDING_KEY))
    stage = _cloud_stage(import_id, receipt) if receipt is not None else None
    staged = (vault_root / STAGE_DIR / import_id).exists()
    problems = ["the receipt of this window cannot be read"] if receipt_damaged else []
    if receipt is not None and RESIDUAL_KEY in receipt:
        if (kept := _record_of(receipt[RESIDUAL_KEY])) is not None:
            return WindowRecord(WINDOW_RESIDUAL, None, tuple(kept[0]), tuple(kept[1]), RECEIPT_SOURCE, renames_pending=pending)
        problems.append("its residual record in the receipt is damaged")
    if receipt is not None and MEMBERS_KEY in receipt and stage is None and receipt["request"].get("kind") == "adopt":
        problems.append("its manifest is damaged")
    walked = _record_of(walk)
    if walk_damaged or (walk is not None and MEMBERS_KEY in walk and walked is None):
        problems.append("its walk record cannot be read")
    if walked is not None and (walked[0] or walked[1] or stage is None):
        return WindowRecord(WINDOW_RESIDUAL, None, tuple(walked[0]), tuple(walked[1]), WALK_SOURCE, renames_pending=pending)
    if stage is not None and not problems:
        return WindowRecord(WINDOW_LANDED, stage, staged=staged, renames_pending=pending)
    if problems or staged:
        return WindowRecord(WINDOW_UNREADABLE, problem="; ".join(problems) or "the receipt of this window cannot be read",
                            staged=staged, renames_pending=pending)
    return None


def read_residual(vault_root: Path, import_id: str) -> list[dict] | None:
    """The unfiled members of a discarded window's residual record, or None when the window has none to read.

    None covers a window with no record, a landed window, and a record that cannot be used; `window_record`
    tells the three apart.
    """
    record = window_record(vault_root, import_id)
    return list(record.members) if record is not None and record.kind == WINDOW_RESIDUAL else None


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


def clear_residual(vault_root: Path, import_id: str, paths: set[str], at: str) -> list[str] | None:
    """Move the named members of a window's residual record under `acknowledged`, stamped `at`; return their paths.

    None means the window has no residual record. A record that cannot be read raises ValueError. The record
    is written where `window_record` read it, in one write, so the status and the acknowledgement act on the
    same record. A path that the record does not list changes nothing.
    """
    record = window_record(vault_root, import_id)
    if record is None or record.kind == WINDOW_LANDED:
        return None
    if record.kind == WINDOW_UNREADABLE:
        raise ValueError(record.problem)
    if record.source == RECEIPT_SOURCE:
        document = read_receipt(vault_root, import_id)
        assert document is not None
        held, dirname = document[RESIDUAL_KEY], CLOUD_RECEIPTS
    else:
        document = held = read_walk(vault_root, import_id)
        assert document is not None
        dirname = WALK_RECORDS
    moved = _acknowledge(held, paths, at)
    if moved:
        _store_record(vault_root, dirname, import_id, document)
    return moved


def _acknowledge(record: dict, paths: set[str], at: str) -> list[str]:
    """Move the members whose path is in `paths` from `record` to its `acknowledged` list, in place; return their paths."""
    kept = [entry for entry in record[MEMBERS_KEY] if entry["path"] not in paths]
    moved = [{**entry, "at": at} for entry in record[MEMBERS_KEY] if entry["path"] in paths]
    record[MEMBERS_KEY] = kept
    record[ACKNOWLEDGED_KEY] = [*record.get(ACKNOWLEDGED_KEY, []), *moved]
    return [entry["path"] for entry in moved]


def _store_record(vault_root: Path, dirname: str, import_id: str, record: dict) -> None:
    """Replace one record whole and durably, as the Job writes it."""
    store = private_state.store(vault_root, dirname)
    private_state.write_record(store, import_id, record, temp_suffix=".tmp", durable=True)
    private_state.sync_directory(store)
