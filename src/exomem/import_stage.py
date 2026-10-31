"""The member manifest of a staged import: which files of `_Imports/<import_id>/` adoption may remove.

Two writers record the same list, one for each route that stages a folder: the Cloud import Job in
its receipt, and `exomem import` in a stage record when an import finishes. Both live in the vault's
state folder, outside the vault. A restore, a moved vault and a hand-made `_Imports/` folder have
none, so nothing in them is consumable.
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from pathlib import Path

from . import private_state, state_migration, state_paths, vault

STAGE_DIR = vault.IMPORT_STAGE_DIRNAME
CLOUD_RECEIPTS = "cloud-imports"
LOCAL_RECORDS = "local-imports"
# What a run records as the reason its stage is trusted; the two values are a closed protocol pair.
CLOUD_PROOF = "cloud-receipt"
LOCAL_PROOF = "local-record"
MEMBERS_KEY = "members"


def member(path: str, size: int, sha256: str) -> dict:
    """One manifest entry: the file's path relative to `_Imports/<import_id>/`, its size and SHA-256."""
    return {"path": path, "size": size, "sha256": sha256}


@dataclass(frozen=True, slots=True)
class Stage:
    """A stage with a manifest: `members` maps each listed path to its (size, SHA-256)."""

    import_id: str
    proof: str
    members: dict[str, tuple[int, str]]


def _is_import_id(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return str(uuid.UUID(value)) == value
    except ValueError:
        return False


def _read_members(entries: object) -> dict[str, tuple[int, str]] | None:
    """The manifest as a map, or None when it is malformed: a manifest that cannot be trusted lists nothing."""
    if not isinstance(entries, list):
        return None
    members: dict[str, tuple[int, str]] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            return None
        path, size, digest = entry.get("path"), entry.get("size"), entry.get("sha256")
        if (not isinstance(path, str) or not isinstance(digest, str) or path in members
                or not isinstance(size, int) or isinstance(size, bool) or size < 0):
            return None
        members[path] = (size, digest)
    return members


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
    if not _is_import_id(import_id):
        return None
    try:
        state = state_paths.vault_state_dir(vault_root)
    except ValueError:
        return None
    receipt = _load(state / CLOUD_RECEIPTS / f"{import_id}.json")
    if receipt is not None and (receipt.get("request") or {}).get("kind") == "adopt":
        members = _read_members(receipt.get(MEMBERS_KEY))
        if members is not None:
            return Stage(import_id, CLOUD_PROOF, members)
    record = _load(state / LOCAL_RECORDS / f"{import_id}.json")
    if record is not None:
        members = _read_members(record.get(MEMBERS_KEY))
        if members is not None:
            return Stage(import_id, LOCAL_PROOF, members)
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
