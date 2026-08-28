"""Crash-safe relocation of machine-local state out of the vault.

The manifest is a durable state machine. It binds one vault identity to the
exact external descriptor set and publishes a family only after every member
has been copied, fsynced, and verified. Source deletion is reachable only from
that published proof.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import stat
import tempfile
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from . import held_fs, state_paths
from .kbdir import kb_dirname

log = logging.getLogger("exomem.state_migration")

MANIFEST_NAME = ".state-migration.json"
MANIFEST_VERSION = 1
_SUPPORTED_MANIFEST_VERSIONS = frozenset({1, 2})
_ROLLBACK_PHASES = frozenset({"prepared", "receipt-committed", "legacy-aligned", "complete"})
_ROLLBACK_PHASE_ORDER = ("prepared", "receipt-committed", "legacy-aligned", "complete")
_ROLLBACK_OPERATIONS = frozenset({
    "governance_schema_v4_downmigration",
    "governance_schema_v3_backup_restore",
})
_LOCK_NAME = ".state-migration.lock"
_COPY_CHUNK = 4 * 1024 * 1024
_MANIFEST_STATES = frozenset({"in-progress", "complete"})
_FAMILY_STATES = frozenset({"pending", "published", "complete"})

_ADOPT_CHOICES = ("vault", "external")
_GOVERNANCE_VAULT_ADOPTION = "governance-store=vault"
_REMEDIATION = (
    "stop every process that can write this vault, then run `exomem maintain "
    "--migrate-state --offline --adopt-state external` to keep the external "
    "state root, or use `--adopt-state vault` to keep the in-vault copy"
)


class StatePlacementConflict(RuntimeError):
    """State exists on two authorities and Exomem cannot choose for the user."""

    code = "STATE_PLACEMENT_CONFLICT"

    def __init__(self, in_vault: Path, external: Path, detail: str = "") -> None:
        self.in_vault = Path(in_vault)
        self.external = Path(external)
        log.error(
            "machine-local state conflicts between %s and %s%s",
            self.in_vault,
            self.external,
            f" ({detail})" if detail else "",
        )
        super().__init__(
            "machine-local state exists both under the vault and in the "
            "external state root; neither copy is silently preferred or "
            "deleted — run `exomem doctor` to see both locations, then "
            f"{_REMEDIATION}"
        )


class StateMigrationManifestError(RuntimeError):
    """The migration authority is unreadable, malformed, or incompatible."""

    code = "STATE_MIGRATION_MANIFEST_INVALID"

    def __init__(self, path: Path, detail: str) -> None:
        self.path = Path(path)
        log.error("state migration manifest at %s is invalid: %s", path, detail)
        super().__init__(
            "the state migration manifest is unreadable or invalid; startup "
            "refuses to guess — run `exomem doctor` for the local path"
        )


class StateMigrationOfflineRequired(RuntimeError):
    """Stable path-free refusal for state that needs an offline transition."""

    code = "STATE_MIGRATION_OFFLINE_REQUIRED"

    def __init__(self, detail: str) -> None:
        log.error("machine-local state requires offline migration: %s", detail)
        super().__init__(
            "STATE_MIGRATION_OFFLINE_REQUIRED: machine-local state is not ready; "
            "stop every legacy writer and run `exomem maintain --migrate-state "
            "--offline` before starting Exomem"
        )


_OFFLINE_AUTHORITY_SEAL = object()


class OfflineMigrationAuthority:
    """Opaque assertion that the caller established a real stop window."""

    __slots__ = ("source", "_seal")

    def __init__(self, source: str, *, _seal: object) -> None:
        if _seal is not _OFFLINE_AUTHORITY_SEAL:
            raise StateMigrationOfflineRequired("offline authority was forged")
        self.source = source
        self._seal = _seal


def assert_offline_migration_authority(*, source: str) -> OfflineMigrationAuthority:
    """Assert an externally proven stop window for one explicit migration.

    The migration lock below serializes only new migrators.  It cannot exclude
    an old release that does not know the lock exists, so this assertion is a
    required operational authority rather than evidence derived from the lock.
    """

    normalized = str(source).strip()
    if not normalized:
        raise StateMigrationOfflineRequired("offline authority source is absent")
    return OfflineMigrationAuthority(normalized, _seal=_OFFLINE_AUTHORITY_SEAL)


def _require_offline_authority(authority: object) -> OfflineMigrationAuthority:
    if (
        not isinstance(authority, OfflineMigrationAuthority)
        or authority._seal is not _OFFLINE_AUTHORITY_SEAL
    ):
        raise StateMigrationOfflineRequired("offline authority is absent")
    return authority


@dataclass(frozen=True, slots=True)
class StateResolution:
    state_dir: Path
    migrated: bool
    dual_state: bool


_RESOLUTION_CACHE: dict[str, StateResolution] = {}
_RESOLUTION_LOCK = threading.Lock()


def reset_state_resolution_cache_for_tests() -> None:
    with _RESOLUTION_LOCK:
        _RESOLUTION_CACHE.clear()


def _cache_key(vault_root: Path, state_dir: Path) -> str:
    return (
        os.path.normcase(str(Path(vault_root).resolve(strict=False)))
        + "\0"
        + os.path.normcase(str(state_dir))
    )


def _descriptor_ids() -> tuple[str, ...]:
    from . import reserved_paths

    return tuple(
        sorted(descriptor.id for descriptor in reserved_paths.external_state_descriptors())
    )


def scan_vault_state(vault_root: Path) -> dict[str, tuple[Path, ...]]:
    """Return top-level legacy members for every external-state descriptor."""

    from . import reserved_paths

    kb = Path(vault_root) / kb_dirname()
    external = frozenset(_descriptor_ids())
    found: dict[str, list[Path]] = {}
    try:
        entries = sorted(os.scandir(kb), key=lambda entry: entry.name)
    except FileNotFoundError:
        return {}
    for entry in entries:
        classification = reserved_paths.classify_logical(entry.name)
        descriptor_id = classification.descriptor_id
        if (
            classification.disposition is reserved_paths.PathDisposition.RESERVED
            and descriptor_id in external
        ):
            found.setdefault(descriptor_id, []).append(kb / entry.name)
    return {descriptor_id: tuple(paths) for descriptor_id, paths in found.items()}


def _scan_external_state(state_dir: Path) -> dict[str, tuple[Path, ...]]:
    """Return external members classified by the same closed registry."""

    from . import reserved_paths

    external = frozenset(_descriptor_ids())
    found: dict[str, list[Path]] = {}
    try:
        entries = sorted(os.scandir(state_dir), key=lambda entry: entry.name)
    except FileNotFoundError:
        return {}
    for entry in entries:
        classification = reserved_paths.classify_logical(entry.name)
        descriptor_id = classification.descriptor_id
        if (
            classification.disposition is reserved_paths.PathDisposition.RESERVED
            and descriptor_id in external
        ):
            found.setdefault(descriptor_id, []).append(Path(state_dir) / entry.name)
    return {descriptor_id: tuple(paths) for descriptor_id, paths in found.items()}


def _inventory_member(path: Path) -> dict[str, tuple[str, str | None]]:
    """Inventory exact names, kinds, and file bytes without following aliases."""

    inventory: dict[str, tuple[str, str | None]] = {}

    def visit(current: Path, relative: PurePosixPath) -> None:
        mode = current.lstat().st_mode
        key = relative.as_posix()
        if stat.S_ISLNK(mode):
            raise OSError("state migration descriptor inventory found an unsafe link")
        if stat.S_ISREG(mode):
            inventory[key] = ("file", _destination_digest(current))
            return
        if not stat.S_ISDIR(mode):
            raise OSError("state migration descriptor inventory found an unsafe entry")
        inventory[key] = ("directory", None)
        with os.scandir(current) as entries:
            children = sorted(entries, key=lambda entry: entry.name)
        for child in children:
            visit(current / child.name, relative / child.name)

    visit(path, PurePosixPath(path.name))
    return inventory


def _assert_descriptor_upgrade_destination_proven(
    vault_root: Path,
    state_dir: Path,
    descriptor_id: str,
    sources: tuple[Path, ...],
    destinations: tuple[Path, ...],
) -> None:
    """Refuse destination-only bytes absent from an older manifest's proof."""

    if not destinations:
        return
    source_inventory: dict[str, tuple[str, str | None]] = {}
    for source in sources:
        source_inventory.update(_inventory_member(source))
    destination_inventory: dict[str, tuple[str, str | None]] = {}
    for destination in destinations:
        destination_inventory.update(_inventory_member(destination))
    destination_is_proven = bool(source_inventory) and all(
        source_inventory.get(relative) == entry
        for relative, entry in destination_inventory.items()
    )
    if not destination_is_proven:
        representative = destinations[0]
        raise StatePlacementConflict(
            Path(vault_root) / kb_dirname() / representative.name,
            representative,
            f"descriptor {descriptor_id!r} has external members not established "
            "by published proof or matching legacy source",
        )


def migration_completed(vault_root: Path) -> bool:
    manifest = _load_manifest(state_paths.vault_state_dir(vault_root), vault_root=vault_root)
    return manifest is not None and manifest["state"] == "complete"


def migration_status(vault_root: Path) -> str:
    """Return cached or manifest-only state suitable for public `/health`.

    Leftover and external-root enumeration belongs to startup admission and
    doctor.  A public liveness request must stay O(1) in both vault size and
    state-root entry count.
    """

    state_dir = state_paths.vault_state_dir(vault_root)
    key = _cache_key(vault_root, state_dir)
    with _RESOLUTION_LOCK:
        cached = _RESOLUTION_CACHE.get(key)
    if cached is not None:
        return "conflict" if cached.dual_state else "complete"
    try:
        manifest = _load_manifest(state_dir, vault_root=vault_root)
    except StateMigrationManifestError:
        return "invalid"
    if manifest is None:
        return "absent"
    state = str(manifest["state"])
    if state == "complete" and tuple(manifest["descriptors"]) != _descriptor_ids():
        return "stale"
    return state


def require_vault_state_ready(vault_root: Path) -> StateResolution:
    """Read-only admission gate for service and stateful CLI startup.

    This gate never creates a directory, takes the migration lock, copies,
    unlinks, resumes, adopts, or upgrades state.  Any state that needs one of
    those transitions receives the same stable offline-required refusal.
    """

    vault_root = Path(vault_root)
    state_dir = state_paths.vault_state_dir(vault_root)
    key = _cache_key(vault_root, state_dir)
    with _RESOLUTION_LOCK:
        cached = _RESOLUTION_CACHE.get(key)

    try:
        state_paths.validate_hosted_state_directory(state_dir)
    except FileNotFoundError as error:
        raise StateMigrationOfflineRequired("hosted state directory is absent") from error

    manifest = _load_manifest(state_dir, vault_root=vault_root)
    if manifest is None:
        raise StateMigrationOfflineRequired("migration manifest is absent")
    if (
        manifest.get("governance_rollback") is not None
        or manifest.get("governance_adoption") is not None
    ):
        raise StateMigrationOfflineRequired("governance rollback marker requires explicit adoption")
    if manifest["state"] != "complete":
        raise StateMigrationOfflineRequired("migration manifest is in progress")
    if tuple(manifest["descriptors"]) != _descriptor_ids():
        raise StateMigrationOfflineRequired("migration manifest descriptor set is stale")
    if scan_vault_state(vault_root):
        raise StateMigrationOfflineRequired("legacy in-vault state is still present")

    if cached is not None:
        return cached
    resolution = StateResolution(state_dir, migrated=True, dual_state=False)
    with _RESOLUTION_LOCK:
        _RESOLUTION_CACHE[key] = resolution
    return resolution


def migrate_vault_state_offline(
    vault_root: Path,
    *,
    authority: object,
    adopt: str | None = None,
) -> StateResolution:
    """Mutate state only under an explicitly asserted offline stop window."""

    _require_offline_authority(authority)
    if adopt is not None and adopt not in (*_ADOPT_CHOICES, _GOVERNANCE_VAULT_ADOPTION):
        raise ValueError(f"--adopt-state must be one of {_ADOPT_CHOICES}")
    vault_root = Path(vault_root)
    state_dir = state_paths.vault_state_dir(vault_root)
    if adopt == _GOVERNANCE_VAULT_ADOPTION:
        return _adopt_governance_store_from_vault_offline(vault_root)
    if adopt is not None:
        existing = _load_manifest(state_dir, vault_root=vault_root)
        if existing is not None and existing.get("version") == 2:
            raise StateMigrationOfflineRequired(
                "rollback-marked state requires governance-store=vault adoption"
            )
        _adopt_state_offline(vault_root, adopt)
        if adopt == "external":
            reset_state_resolution_cache_for_tests()
            return require_vault_state_ready(vault_root)

    state_paths.ensure_vault_state_dir(vault_root)
    with _migration_lock(state_dir):
        resolution = _resolve_locked(vault_root, state_dir)
    if resolution.dual_state:
        raise StatePlacementConflict(
            vault_root / kb_dirname(),
            state_dir,
            "a complete manifest has legacy in-vault duplicates",
        )
    require_vault_state_ready(vault_root)
    return resolution


def _resolve_locked(vault_root: Path, state_dir: Path) -> StateResolution:
    descriptor_ids = _descriptor_ids()
    manifest = _load_manifest(state_dir, vault_root=vault_root)
    leftovers = scan_vault_state(vault_root)
    started_without_manifest = manifest is None

    if manifest is None:
        if _external_state_present(state_dir):
            raise StatePlacementConflict(
                vault_root / kb_dirname(),
                state_dir,
                "the external root has no migration manifest",
            )
        manifest = _new_manifest(vault_root, descriptor_ids)
        _write_manifest(state_dir, manifest)
    else:
        if manifest.get("governance_rollback") is not None or manifest.get("governance_adoption") is not None:
            raise StateMigrationOfflineRequired("rollback marker requires its governance coordinator")
        recorded = tuple(manifest["descriptors"])
        unknown = sorted(set(recorded) - set(descriptor_ids))
        if unknown:
            raise StateMigrationManifestError(
                _manifest_path(state_dir), "manifest names unknown descriptors"
            )
        missing = sorted(set(descriptor_ids) - set(recorded))
        if manifest["state"] == "complete" and not missing:
            return StateResolution(state_dir, migrated=True, dual_state=bool(leftovers))
        if missing:
            external = _scan_external_state(state_dir)
            for descriptor_id in missing:
                _assert_descriptor_upgrade_destination_proven(
                    vault_root,
                    state_dir,
                    descriptor_id,
                    leftovers.get(descriptor_id, ()),
                    external.get(descriptor_id, ()),
                )
            families = manifest["families"]
            for descriptor_id in recorded:
                families.setdefault(descriptor_id, {"status": "complete"})
            for descriptor_id in missing:
                families[descriptor_id] = {"status": "pending"}
            manifest["descriptors"] = list(descriptor_ids)
            manifest["state"] = "in-progress"
            _write_manifest(state_dir, manifest)

    for descriptor_id in descriptor_ids:
        manifest = _load_manifest(state_dir, vault_root=vault_root)
        assert manifest is not None
        family = manifest["families"].setdefault(
            descriptor_id, {"status": "pending"}
        )
        status_value = family["status"]
        if status_value == "complete":
            continue
        members = scan_vault_state(vault_root).get(descriptor_id, ())
        if status_value == "pending" and not members:
            family.clear()
            family["status"] = "complete"
            _write_manifest(state_dir, manifest)
            continue
        _move_family(vault_root, state_dir, descriptor_id, members)

    manifest = _load_manifest(state_dir, vault_root=vault_root)
    assert manifest is not None
    manifest["descriptors"] = list(descriptor_ids)
    manifest["state"] = "complete"
    _write_manifest(state_dir, manifest)
    remaining = scan_vault_state(vault_root)
    moved = started_without_manifest and bool(leftovers)
    log.info("machine-local state placement ready: %d legacy families", len(leftovers))
    return StateResolution(
        state_dir,
        migrated=moved or not started_without_manifest,
        dual_state=bool(remaining),
    )


def _new_manifest(vault_root: Path, descriptor_ids: tuple[str, ...]) -> dict[str, Any]:
    return {
        "version": MANIFEST_VERSION,
        "vault_identity": state_paths.vault_state_key(vault_root),
        "descriptors": list(descriptor_ids),
        "state": "in-progress",
        "families": {
            descriptor_id: {"status": "pending"} for descriptor_id in descriptor_ids
        },
    }


def _move_family(
    vault_root: Path,
    state_dir: Path,
    descriptor_id: str,
    members: tuple[Path, ...],
) -> None:
    """Publish one descriptor family as a verified all-member group."""

    manifest = _load_manifest(state_dir, vault_root=vault_root)
    if manifest is None:
        raise StateMigrationManifestError(_manifest_path(state_dir), "manifest vanished")
    family = manifest["families"].get(descriptor_id)
    if not isinstance(family, dict):
        raise StateMigrationManifestError(
            _manifest_path(state_dir), "family state is unavailable"
        )

    acquired = held_fs.acquire(Path(vault_root))
    if not acquired.ok:
        raise OSError("state migration cannot acquire the vault")
    with acquired.require() as filesystem:
        if family["status"] == "published":
            records = _published_records(family)
        else:
            records: dict[str, dict[str, Any]] = {}
            for member in members:
                for source_relative in _family_source_files(filesystem, member):
                    record = _publish_source_file(
                        filesystem,
                        vault_root=Path(vault_root),
                        state_dir=Path(state_dir),
                        source_relative=source_relative,
                    )
                    records[source_relative.as_posix()] = record
            family.clear()
            family.update({"status": "published", "members": records})
            _write_manifest(state_dir, manifest)
            _crash_point("after-manifest-publish")
            _fsync_directory(state_dir)
            _crash_point("after-directory-fsync")

        _verify_published_records(state_dir, records)
        for member_name, record in records.items():
            deleted = _delete_published_member(
                filesystem,
                vault_root,
                member_name,
                record,
            )
            if deleted:
                _crash_point("after-source-delete")
        removed_tree = False
        for member in members:
            if member.is_dir() and not member.is_symlink():
                relative = f"{kb_dirname()}/{member.name}"
                removed_tree = _remove_empty_tree(filesystem, relative) or removed_tree
        # Fence the top-level legacy namespace on every published-family
        # replay, including when a prior unlink succeeded but its parent flush
        # failed and the tree is not visible on this retry.  Only after this
        # durable absence proof may the family advance to complete.
        kb_parent_result = filesystem.parent(kb_dirname(), access="flush")
        if not kb_parent_result.ok:
            raise OSError("state migration cannot retain the legacy state root")
        with kb_parent_result.require() as kb_parent:
            flushed = filesystem.flush_directory(kb_parent)
            if not flushed.ok:
                raise OSError("state migration could not flush the legacy state root")
        if removed_tree:
            _crash_point("after-tree-removal")
    manifest = _load_manifest(state_dir, vault_root=vault_root)
    assert manifest is not None
    family = manifest["families"][descriptor_id]
    family.clear()
    family["status"] = "complete"
    _write_manifest(state_dir, manifest)


def _family_source_files(
    filesystem: held_fs.HeldFilesystem,
    member: Path,
) -> tuple[Path, ...]:
    if member.is_dir() and not member.is_symlink():
        relative = f"{kb_dirname()}/{member.name}"
        parent_result = filesystem.parent(relative)
        if not parent_result.ok:
            if parent_result.error is not None and parent_result.error.code == "MISSING":
                return ()
            raise OSError("state migration cannot retain a legacy state tree")
        with parent_result.require() as directory:
            enumerated = filesystem.enumerate(directory)
            if not enumerated.ok:
                raise OSError("state migration cannot enumerate a legacy state tree")
            return tuple(
                Path(member.name) / Path(record.relative_path)
                for record in enumerated.require()
                if record.identity.kind == "file"
            )
    return (Path(member.name),)


def _publish_source_file(
    filesystem: held_fs.HeldFilesystem,
    *,
    vault_root: Path,
    state_dir: Path,
    source_relative: Path,
) -> dict[str, Any]:
    parent_relative = source_relative.parent.as_posix()
    held_parent = kb_dirname()
    if parent_relative != ".":
        held_parent = f"{held_parent}/{parent_relative}"
    parent_result = filesystem.parent(held_parent)
    if not parent_result.ok:
        raise OSError("state migration cannot retain a legacy state parent")
    with parent_result.require() as parent:
        file_result = filesystem.file(parent, source_relative.name)
        if not file_result.ok:
            if file_result.error is not None and file_result.error.code == "MISSING":
                raise OSError("state migration source changed during enumeration")
            raise OSError("state migration cannot retain a legacy state file")
        with file_result.require() as source:
            descriptor = getattr(source, "descriptor", None)
            if not isinstance(descriptor, int):
                raise OSError("state migration cannot stream a legacy state file")
            identity = source.identity
            destination = state_dir / source_relative
            _ensure_safe_destination_parent(state_dir, destination.parent)
            existed = _regular_destination_exists(destination)
            if existed:
                source_digest = _descriptor_digest(descriptor)
            else:
                source_digest = _stage_copy(descriptor, destination)
                _crash_point("after-copy")
            destination_digest = _destination_digest(destination)
            if destination_digest != source_digest:
                if not existed:
                    _discard(destination)
                if existed:
                    raise StatePlacementConflict(
                        vault_root / kb_dirname() / source_relative,
                        destination,
                        "the two copies differ",
                    )
                raise OSError(
                    "state migration verification failed; the source was not deleted"
                )
            _crash_point("after-verification")
            return {
                "source": source_relative.as_posix(),
                "destination": source_relative.as_posix(),
                "sha256": source_digest,
                "size": destination.stat().st_size,
                "identity": [
                    identity.device,
                    identity.inode,
                    identity.kind,
                    identity.link_count,
                ],
            }


def _ensure_safe_destination_parent(state_dir: Path, parent: Path) -> None:
    try:
        relative = parent.relative_to(state_dir)
    except ValueError as error:
        raise OSError("state migration destination escaped the state root") from error
    current = state_dir
    for part in relative.parts:
        current = current / part
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            current.mkdir()
            _fsync_directory(current.parent)
            continue
        if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
            raise OSError("state migration destination parent is unsafe")


def _regular_destination_exists(path: Path) -> bool:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
        raise OSError("state migration destination is unsafe")
    return True


def _verify_published_records(
    state_dir: Path,
    records: Mapping[str, Mapping[str, Any]],
) -> None:
    for member_name, record in records.items():
        if not _valid_member_record(record, member_name=member_name):
            raise StateMigrationManifestError(
                _manifest_path(state_dir), "published member proof is invalid"
            )
        destination = _contained_member_path(
            state_dir,
            str(record["destination"]),
        )
        if not _regular_destination_exists(destination):
            raise OSError("published state migration destination is missing")
        if destination.stat().st_size != record["size"]:
            raise OSError("published state migration destination changed")
        if _destination_digest(destination) != record["sha256"]:
            raise OSError("published state migration destination changed")


def _delete_published_member(
    filesystem: held_fs.HeldFilesystem,
    vault_root: Path,
    member_name: str,
    record: Mapping[str, Any],
) -> bool:
    """Delete one source only when identity and bytes match its durable proof."""

    if not _valid_member_record(record, member_name=member_name):
        raise StateMigrationManifestError(
            Path(MANIFEST_NAME), "published member proof is invalid"
        )
    source = str(record["source"])
    _contained_member_path(Path(vault_root) / kb_dirname(), source)
    relative = Path(*PurePosixPath(source).parts)
    parent_relative = relative.parent.as_posix()
    held_parent = kb_dirname()
    if parent_relative != ".":
        held_parent = f"{held_parent}/{parent_relative}"
    parent_result = filesystem.parent(held_parent, access="flush")
    if not parent_result.ok:
        if parent_result.error is not None and parent_result.error.code == "MISSING":
            return False
        raise OSError("state migration cannot retain a published source parent")
    with parent_result.require() as parent:
        file_result = filesystem.file(parent, relative.name, access="mutate")
        if not file_result.ok:
            if file_result.error is not None and file_result.error.code == "MISSING":
                return False
            raise OSError("state migration cannot re-acquire a published source")
        with file_result.require() as current:
            identity = current.identity
            observed = [
                identity.device,
                identity.inode,
                identity.kind,
                identity.link_count,
            ]
            if observed != record["identity"]:
                raise OSError("state migration source identity changed after publication")
            descriptor = getattr(current, "descriptor", None)
            if (
                not isinstance(descriptor, int)
                or _descriptor_digest(descriptor) != record["sha256"]
            ):
                raise OSError("state migration source bytes changed after publication")
            removed = filesystem.unlink(current)
            if not removed.ok:
                raise OSError("state migration could not remove a published source")
        flushed = filesystem.flush_directory(parent)
        if not flushed.ok:
            raise OSError("state migration could not flush the source directory")
        return True


def _remove_empty_tree(
    filesystem: held_fs.HeldFilesystem,
    relative: str,
) -> bool:
    """Durably remove one now-empty tree through retained directory handles."""

    opened = filesystem.parent(relative)
    if not opened.ok:
        if opened.error is not None and opened.error.code == "MISSING":
            return False
        raise OSError("state migration cannot retain a legacy state tree")
    with opened.require() as root:
        enumerated = filesystem.enumerate(root)
        if not enumerated.ok:
            raise OSError("state migration cannot enumerate a legacy state tree")
        records = enumerated.require()
    if any(record.identity.kind != "directory" for record in records):
        raise OSError("state migration legacy state tree is not empty")

    descendants = sorted(
        (f"{relative}/{record.relative_path}" for record in records),
        key=lambda value: (value.count("/"), value),
        reverse=True,
    )
    removed = False
    for directory_relative in (*descendants, relative):
        parent_relative, _, _leaf = directory_relative.rpartition("/")
        parent_result = filesystem.parent(parent_relative or ".", access="flush")
        if not parent_result.ok:
            raise OSError("state migration cannot retain a state-tree parent")
        with parent_result.require() as parent:
            directory_result = filesystem.parent(directory_relative, access="mutate")
            if not directory_result.ok:
                if (
                    directory_result.error is not None
                    and directory_result.error.code == "MISSING"
                ):
                    continue
                raise OSError("state migration cannot retain an empty state directory")
            with directory_result.require() as directory:
                children = filesystem.children(directory)
                if not children.ok:
                    raise OSError("state migration cannot validate an empty state directory")
                if children.require():
                    raise OSError("state migration legacy state tree is not empty")
                unlinked = filesystem.unlink_directory(directory)
                if not unlinked.ok:
                    raise OSError("state migration could not remove an empty state directory")
            flushed = filesystem.flush_directory(parent)
            if not flushed.ok:
                raise OSError("state migration could not flush a state-tree parent")
            removed = True
    return removed


def _stage_copy(descriptor: int, destination: Path) -> str:
    digest = hashlib.sha256()
    os.lseek(descriptor, 0, os.SEEK_SET)
    handle_fd, staged_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".migrating", dir=destination.parent
    )
    try:
        with os.fdopen(handle_fd, "wb") as staged:
            while chunk := os.read(descriptor, _COPY_CHUNK):
                digest.update(chunk)
                staged.write(chunk)
            staged.flush()
            os.fsync(staged.fileno())
        os.replace(staged_name, destination)
        _fsync_directory(destination.parent)
    except BaseException:
        try:
            os.unlink(staged_name)
        except OSError:
            pass
        raise
    return digest.hexdigest()


def _descriptor_digest(descriptor: int) -> str:
    digest = hashlib.sha256()
    os.lseek(descriptor, 0, os.SEEK_SET)
    while chunk := os.read(descriptor, _COPY_CHUNK):
        digest.update(chunk)
    return digest.hexdigest()


def _destination_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_COPY_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def _discard(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def _crash_point(_point: str) -> None:
    """Failure-injection seam at each durable migration cut."""


def _manifest_path(state_dir: Path) -> Path:
    return Path(state_dir) / MANIFEST_NAME


def _read_manifest_bytes(path: Path) -> bytes:
    return path.read_bytes()


def _load_manifest(
    state_dir: Path,
    *,
    vault_root: Path | None = None,
) -> dict[str, Any] | None:
    path = _manifest_path(state_dir)
    try:
        raw = _read_manifest_bytes(path)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise StateMigrationManifestError(path, "manifest is unreadable") from error
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise StateMigrationManifestError(path, "manifest is not valid JSON") from error
    _validate_manifest(path, payload, vault_root=vault_root)
    return payload


def _validate_manifest(
    path: Path,
    payload: object,
    *,
    vault_root: Path | None,
) -> None:
    if not isinstance(payload, dict):
        raise StateMigrationManifestError(path, "manifest must be an object")
    if payload.get("version") not in _SUPPORTED_MANIFEST_VERSIONS:
        raise StateMigrationManifestError(path, "manifest version is unsupported")
    identity = payload.get("vault_identity")
    if not isinstance(identity, str) or not identity:
        raise StateMigrationManifestError(path, "vault identity is invalid")
    if vault_root is not None and identity != state_paths.vault_state_key(vault_root):
        raise StateMigrationManifestError(path, "vault identity does not match")
    descriptors = payload.get("descriptors")
    if (
        not isinstance(descriptors, list)
        or not all(isinstance(item, str) and item for item in descriptors)
        or descriptors != sorted(set(descriptors))
    ):
        raise StateMigrationManifestError(path, "descriptor set is invalid")
    if payload.get("state") not in _MANIFEST_STATES:
        raise StateMigrationManifestError(path, "migration state is invalid")
    families = payload.get("families")
    if not isinstance(families, dict):
        raise StateMigrationManifestError(path, "family states are invalid")
    for descriptor_id, family in families.items():
        if descriptor_id not in descriptors or not isinstance(family, dict):
            raise StateMigrationManifestError(path, "family descriptor is invalid")
        status_value = family.get("status")
        if status_value not in _FAMILY_STATES:
            raise StateMigrationManifestError(path, "family status is invalid")
        if status_value != "published":
            continue
        members = family.get("members")
        if not isinstance(members, dict):
            raise StateMigrationManifestError(path, "published member set is invalid")
        for name, record in members.items():
            if not isinstance(name, str) or not _valid_member_record(
                record,
                member_name=name,
            ):
                raise StateMigrationManifestError(path, "published member proof is invalid")
    if payload["state"] == "complete" and (
        set(families) != set(descriptors)
        or any(family.get("status") != "complete" for family in families.values())
    ):
        raise StateMigrationManifestError(path, "complete manifest has incomplete families")
    rollback = payload.get("governance_rollback")
    adoption = payload.get("governance_adoption")
    if payload["version"] == 1 and rollback is not None:
        raise StateMigrationManifestError(path, "v1 manifest carries a rollback marker")
    if payload["version"] == 1 and adoption is not None:
        raise StateMigrationManifestError(path, "v1 manifest carries a governance adoption")
    if payload["version"] == 2 and rollback is not None:
        if not isinstance(rollback, dict) or set(rollback) != {
            "operation", "event_id", "phase", "plan_digest", "target_digest",
            "timestamp", "d0", "legacy_path", "stage_leaf", "backup_reference",
            "backup_plan_digest", "source_store_digest", "schema_fence_generation", "d1", "terminal",
        }:
            raise StateMigrationManifestError(path, "governance rollback marker is invalid")
        if rollback["phase"] not in _ROLLBACK_PHASES or rollback["operation"] not in _ROLLBACK_OPERATIONS:
            raise StateMigrationManifestError(path, "governance rollback phase is invalid")
        if (
            not _is_hex64(rollback["event_id"])
            or not all(_is_hex64(rollback[key]) for key in ("plan_digest", "target_digest", "d0"))
            or rollback["legacy_path"] != f"{kb_dirname()}/.governance.sqlite"
            or rollback["stage_leaf"] != f".governance-v3-rollback-{rollback['event_id']}.sqlite"
            or not isinstance(rollback["timestamp"], int)
            or isinstance(rollback["timestamp"], bool)
            or rollback["timestamp"] < 1
        ):
            raise StateMigrationManifestError(path, "governance rollback binding is invalid")
        backup_reference = rollback["backup_reference"]
        if rollback["operation"] == "governance_schema_v4_downmigration":
            if any(
                rollback[key] is not None
                for key in ("backup_reference", "backup_plan_digest", "source_store_digest")
            ):
                raise StateMigrationManifestError(path, "downmigration marker carries a backup reference")
        elif (
            not _valid_backup_reference(backup_reference)
            or not _is_hex64(rollback["backup_plan_digest"])
            or not _is_hex64(rollback["source_store_digest"])
        ):
            raise StateMigrationManifestError(path, "backup restore marker lacks its backup reference")
        generation = rollback["schema_fence_generation"]
        if generation is not None and (
            not isinstance(generation, int) or isinstance(generation, bool) or generation < 1
        ):
            raise StateMigrationManifestError(path, "governance rollback fence generation is invalid")
        if rollback["phase"] == "prepared" and (
            rollback["d1"] is not None or rollback["terminal"] is not None
        ):
            raise StateMigrationManifestError(path, "prepared rollback marker carries a terminal endpoint")
        if rollback["phase"] in {"receipt-committed", "legacy-aligned", "complete"} and (
            not _is_hex64(rollback["d1"])
            or not isinstance(rollback["terminal"], dict)
            or set(rollback["terminal"]) != {"instance_id", "seq", "hash", "path", "byte_offset"}
            or not isinstance(rollback["terminal"].get("instance_id"), str)
            or len(rollback["terminal"]["instance_id"]) != 32
            or any(character not in "0123456789abcdef" for character in rollback["terminal"]["instance_id"])
            or not isinstance(rollback["terminal"].get("seq"), int)
            or isinstance(rollback["terminal"]["seq"], bool)
            or rollback["terminal"]["seq"] < 1
            or not _is_hex64(rollback["terminal"].get("hash"))
            or not isinstance(rollback["terminal"].get("path"), str)
            or not _valid_rollback_endpoint_path(
                rollback["terminal"]["instance_id"], rollback["terminal"]["path"]
            )
            or not isinstance(rollback["terminal"].get("byte_offset"), int)
            or isinstance(rollback["terminal"]["byte_offset"], bool)
            or rollback["terminal"]["byte_offset"] < 0
        ):
            raise StateMigrationManifestError(path, "governance rollback endpoint is invalid")
    if adoption is not None:
        if payload["version"] != 2 or not isinstance(rollback, dict) or rollback.get("phase") != "complete":
            raise StateMigrationManifestError(path, "governance adoption lacks a complete rollback marker")
        if not isinstance(adoption, dict) or set(adoption) != {"phase", "event_id", "d1", "legacy_digest"}:
            raise StateMigrationManifestError(path, "governance adoption record is invalid")
        if (
            adoption.get("phase") not in {"prepared", "copied"}
            or adoption.get("event_id") != rollback.get("event_id")
            or adoption.get("d1") != rollback.get("d1")
            or not _is_hex64(adoption.get("legacy_digest"))
        ):
            raise StateMigrationManifestError(path, "governance adoption binding is invalid")


def _is_hex64(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _is_hex32(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 32
        and all(character in "0123456789abcdef" for character in value)
    )


def _valid_backup_reference(value: object) -> bool:
    prefix = "exomem-governance-v3-backup://sha256/"
    return isinstance(value, str) and value.startswith(prefix) and _is_hex64(value[len(prefix):])


def _valid_rollback_endpoint_path(instance_id: object, value: object) -> bool:
    if not _is_hex32(instance_id):
        return False
    if not isinstance(value, str):
        return False
    prefix = f"{kb_dirname()}/_Governance/events/{instance_id}/"
    leaf = value.removeprefix(prefix)
    return (
        value.startswith(prefix)
        and len(leaf) == len("2026-08.jsonl")
        and leaf[4:5] == "-"
        and leaf[7:] == ".jsonl"
        and leaf[:4].isdigit()
        and leaf[5:7].isdigit()
        and 1 <= int(leaf[5:7]) <= 12
    )


def _normalized_member_name(value: object) -> str | None:
    """Return one unambiguous relative POSIX member name, else ``None``."""

    if not isinstance(value, str) or not value or "\\" in value:
        return None
    relative = PurePosixPath(value)
    if relative.is_absolute() or relative.as_posix() != value:
        return None
    if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        return None
    return value


def _contained_member_path(root: Path, value: object) -> Path:
    """Resolve a manifest member only when lexical and real paths stay below root."""

    member = _normalized_member_name(value)
    if member is None:
        raise StateMigrationManifestError(
            Path(MANIFEST_NAME), "published member proof is invalid"
        )
    root = Path(root)
    target = root.joinpath(*PurePosixPath(member).parts)
    lexical_root = Path(os.path.abspath(root))
    lexical_target = Path(os.path.abspath(target))
    resolved_root = root.resolve(strict=False)
    resolved_target = target.resolve(strict=False)
    try:
        lexical_target.relative_to(lexical_root)
        resolved_target.relative_to(resolved_root)
    except ValueError as error:
        raise StateMigrationManifestError(
            Path(MANIFEST_NAME), "published member escaped its state root"
        ) from error
    return target


def _valid_member_record(
    record: object,
    *,
    member_name: str | None = None,
) -> bool:
    if not isinstance(record, dict):
        return False
    source = _normalized_member_name(record.get("source"))
    destination = _normalized_member_name(record.get("destination"))
    normalized_key = (
        _normalized_member_name(member_name) if member_name is not None else None
    )
    digest = record.get("sha256")
    identity = record.get("identity")
    return bool(
        source is not None
        and destination is not None
        and (member_name is None or normalized_key == source == destination)
        and isinstance(digest, str)
        and len(digest) == 64
        and all(character in "0123456789abcdef" for character in digest)
        and isinstance(record.get("size"), int)
        and record["size"] >= 0
        and isinstance(identity, list)
        and len(identity) == 4
        and isinstance(identity[0], int)
        and isinstance(identity[1], int)
        and identity[2] == "file"
        and isinstance(identity[3], int)
    )


def _published_records(family: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    members = family.get("members")
    if not isinstance(members, dict):
        raise StateMigrationManifestError(Path(MANIFEST_NAME), "published proof vanished")
    records: dict[str, dict[str, Any]] = {}
    for name, record in members.items():
        if (
            not isinstance(name, str)
            or not isinstance(record, dict)
            or not _valid_member_record(record, member_name=name)
        ):
            raise StateMigrationManifestError(
                Path(MANIFEST_NAME), "published member proof is invalid"
            )
        records[name] = dict(record)
    return records


def _write_manifest(state_dir: Path, manifest: Mapping[str, Any]) -> None:
    path = _manifest_path(state_dir)
    _validate_manifest(path, manifest, vault_root=None)
    handle_fd, staged = tempfile.mkstemp(
        prefix=f".{MANIFEST_NAME}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(handle_fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(manifest, handle, sort_keys=True, indent=1)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(staged, path)
        _fsync_directory(path.parent)
    except BaseException:
        try:
            os.unlink(staged)
        except OSError:
            pass
        raise


def _write_manifest_cas(state_dir: Path, manifest: Mapping[str, Any], *, expected_raw: bytes) -> None:
    """Replace the manifest only when the held migration preimage still matches."""
    path = _manifest_path(state_dir)
    try:
        current = _read_manifest_bytes(path)
    except OSError as error:
        raise StateMigrationManifestError(path, "manifest CAS could not read current bytes") from error
    if not hashlib.sha256(current).digest() == hashlib.sha256(expected_raw).digest():
        raise StateMigrationManifestError(path, "manifest changed during rollback transition")
    _write_manifest(state_dir, manifest)


@contextmanager
def governance_rollback_session(vault_root: Path) -> Iterator["GovernanceRollbackSession"]:
    """One held migration-lock session for v1 begin or v2 replay coordinators."""
    root = Path(vault_root)
    state_dir = state_paths.vault_state_dir(root)
    with _migration_lock(state_dir):
        raw = _read_manifest_bytes(_manifest_path(state_dir))
        manifest = _load_manifest(state_dir, vault_root=root)
        if manifest is None or manifest.get("state") != "complete":
            raise StateMigrationOfflineRequired("rollback requires a complete state manifest")
        if manifest.get("version") not in _SUPPORTED_MANIFEST_VERSIONS:
            raise StateMigrationOfflineRequired("rollback manifest version is unsupported")
        yield GovernanceRollbackSession(root, state_dir, manifest, raw)


@dataclass(slots=True)
class GovernanceRollbackSession:
    """Marker state mutated only while ``governance_rollback_session`` holds its lock."""

    root: Path
    state_dir: Path
    manifest: dict[str, Any]
    _raw: bytes
    _binding: dict[str, Any] | None = field(init=False, default=None)

    def __post_init__(self) -> None:
        marker = self._marker()
        if marker is not None:
            self._binding = self._marker_binding(marker)

    @property
    def marker(self) -> dict[str, Any] | None:
        marker = self._marker()
        return None if marker is None else json.loads(json.dumps(marker))

    def _marker(self) -> dict[str, Any] | None:
        value = self.manifest.get("governance_rollback")
        return value if isinstance(value, dict) else None

    @staticmethod
    def _marker_binding(marker: Mapping[str, Any]) -> dict[str, Any]:
        return {
            key: json.loads(json.dumps(marker[key]))
            for key in (
                "operation", "event_id", "plan_digest", "target_digest", "timestamp",
                "d0", "legacy_path", "stage_leaf", "backup_reference", "backup_plan_digest",
                "source_store_digest", "schema_fence_generation", "d1", "terminal",
            )
        }

    def _require_immutable_binding(self) -> None:
        marker = self._marker()
        if marker is None or self._binding is None:
            raise StateMigrationOfflineRequired("rollback marker binding is absent")
        if self._marker_binding(marker) != self._binding:
            raise StateMigrationOfflineRequired("rollback marker binding changed during held session")

    def _cas(self) -> None:
        self._require_immutable_binding()
        _write_manifest_cas(self.state_dir, self.manifest, expected_raw=self._raw)
        self._raw = _read_manifest_bytes(_manifest_path(self.state_dir))

    def begin_prepared(
        self, *, operation: str, event_id: str, plan_digest: str, target_digest: str,
        timestamp: int, d0: str, backup_reference: str | None = None,
        backup_plan_digest: str | None = None, source_store_digest: str | None = None,
        schema_fence_generation: int | None = None,
    ) -> dict[str, Any]:
        if self.manifest.get("version") != 1 or self._marker() is not None:
            raise StateMigrationOfflineRequired("rollback marker is already established")
        marker = {
            "operation": operation, "event_id": event_id, "phase": "prepared",
            "plan_digest": plan_digest, "target_digest": target_digest, "timestamp": timestamp,
            "d0": d0, "legacy_path": f"{kb_dirname()}/.governance.sqlite",
            "stage_leaf": f".governance-v3-rollback-{event_id}.sqlite",
            "backup_reference": backup_reference, "backup_plan_digest": backup_plan_digest,
            "source_store_digest": source_store_digest,
            "schema_fence_generation": schema_fence_generation,
            "d1": None, "terminal": None,
        }
        self.manifest["version"] = 2
        self.manifest["governance_rollback"] = marker
        self._binding = self._marker_binding(marker)
        self._cas()
        return marker

    def _advance(self, phase: str, *, d1: str | None = None, endpoint: Mapping[str, Any] | None = None) -> None:
        marker = self._marker()
        if marker is None:
            raise StateMigrationOfflineRequired("rollback marker is absent")
        current = _ROLLBACK_PHASE_ORDER.index(marker["phase"])
        if _ROLLBACK_PHASE_ORDER.index(phase) != current + 1:
            raise StateMigrationOfflineRequired("rollback marker phase is not monotonic")
        if phase == "receipt-committed":
            if d1 is None or endpoint is None:
                raise StateMigrationOfflineRequired("rollback receipt endpoint is absent")
            marker["d1"] = d1
            marker["terminal"] = dict(endpoint)
            self._binding = self._marker_binding(marker)
        marker["phase"] = phase
        self._cas()

    def advance_receipt_committed(self, d1: str, endpoint: Mapping[str, Any]) -> None:
        self._advance("receipt-committed", d1=d1, endpoint=endpoint)

    def advance_legacy_aligned(self) -> None:
        self._advance("legacy-aligned")

    def seal_complete_metadata_only(self) -> None:
        """Fence-v3 replay seals only immutable marker metadata; never opens legacy."""
        self._advance("complete")


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        from . import mutation_lock

        mutation_lock._windows_flush_directory(path)
        return
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _external_state_present(state_dir: Path) -> bool:
    try:
        entries = os.scandir(state_dir)
    except FileNotFoundError:
        return False
    except OSError as error:
        raise OSError("external state root cannot be inspected") from error
    with entries:
        return any(entry.name not in {MANIFEST_NAME, _LOCK_NAME} for entry in entries)


@contextmanager
def _migration_lock(state_dir: Path) -> Iterator[None]:
    path = Path(state_dir) / _LOCK_NAME
    handle = path.open("a+b")
    locked = False
    try:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
            os.fsync(handle.fileno())
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            locked = True
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            locked = True
        yield
    finally:
        try:
            if locked and os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            elif locked:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def _adopt_state_offline(vault_root: Path, keep: str) -> dict[str, Any]:
    if keep not in _ADOPT_CHOICES:
        raise ValueError(f"--adopt-state must be one of {_ADOPT_CHOICES}")
    vault_root = Path(vault_root)
    state_dir = state_paths.vault_state_dir(vault_root)
    removed: list[str] = []
    if keep == "external":
        state_paths.ensure_vault_state_dir(vault_root)
        with _migration_lock(state_dir):
            leftovers = scan_vault_state(vault_root)
            acquired = held_fs.acquire(vault_root)
            if not acquired.ok:
                raise OSError("adopt-state cannot acquire the vault")
            with acquired.require() as filesystem:
                for descriptor_id in sorted(leftovers):
                    for member in leftovers[descriptor_id]:
                        if member.is_dir() and not member.is_symlink():
                            _adopt_remove_tree(filesystem, member)
                        else:
                            _adopt_remove_file(filesystem, member.name)
                        removed.append(member.name)
            manifest = _new_manifest(vault_root, _descriptor_ids())
            manifest["families"] = {
                descriptor_id: {"status": "complete"}
                for descriptor_id in manifest["descriptors"]
            }
            manifest["state"] = "complete"
            manifest["adopted"] = "external"
            _write_manifest(state_dir, manifest)
    else:
        import shutil

        state_paths.ensure_vault_state_dir(vault_root)
        with _migration_lock(state_dir):
            with os.scandir(state_dir) as entries:
                for entry in entries:
                    if entry.name == _LOCK_NAME:
                        continue
                    path = state_dir / entry.name
                    mode = path.lstat().st_mode
                    if stat.S_ISLNK(mode):
                        raise OSError("adopt-state external root contains an unsafe link")
                    if stat.S_ISDIR(mode):
                        shutil.rmtree(path)
                    elif stat.S_ISREG(mode):
                        path.unlink()
                    else:
                        raise OSError("adopt-state external root contains an unsafe entry")
            _fsync_directory(state_dir)
        removed.append(str(state_dir))
    reset_state_resolution_cache_for_tests()
    return {"kept": keep, "removed": removed, "state_root": str(state_dir)}


def _require_single_regular_file(path: Path, *, detail: str) -> None:
    try:
        entry = Path(path).lstat()
    except FileNotFoundError as error:
        raise StateMigrationOfflineRequired(f"{detail} is absent") from error
    except OSError as error:
        raise StateMigrationOfflineRequired(f"{detail} cannot be inspected") from error
    if (
        stat.S_ISLNK(entry.st_mode)
        or not stat.S_ISREG(entry.st_mode)
        or entry.st_nlink != 1
    ):
        raise StateMigrationOfflineRequired(f"{detail} is not one independent regular file")


def _exact_v3_digest(path: Path, *, detail: str) -> str:
    from .governance import schema_v4, store

    _require_single_regular_file(path, detail=detail)
    try:
        connection = sqlite3.connect(f"{Path(path).resolve().as_uri()}?mode=ro", uri=True)
        try:
            connection.execute("PRAGMA query_only=ON")
            schema_v4.require_exact_v3_connection(connection)
            return store._v3_snapshot_digest(connection)  # noqa: SLF001 - canonical v3 digest
        finally:
            connection.close()
    except StateMigrationOfflineRequired:
        raise
    except (OSError, sqlite3.Error, schema_v4.SchemaV4Error) as error:
        raise StateMigrationOfflineRequired(f"{detail} is not an exact v3 database") from error


@contextmanager
def _retained_legacy_governance_file(
    vault_root: Path,
) -> Iterator[tuple[held_fs.HeldFilesystem, held_fs.HeldDirectory, held_fs.HeldFile]]:
    acquired = held_fs.acquire(Path(vault_root))
    if not acquired.ok:
        raise StateMigrationOfflineRequired("legacy governance store cannot be retained")
    with acquired.require() as filesystem:
        parent_result = filesystem.parent(kb_dirname(), access="read")
        if not parent_result.ok:
            raise StateMigrationOfflineRequired("legacy governance parent cannot be retained")
        with parent_result.require() as parent:
            _require_legacy_sqlite_sidecars_absent(filesystem, parent)
            file_result = filesystem.file(parent, ".governance.sqlite", access="read")
            if not file_result.ok:
                raise StateMigrationOfflineRequired("legacy governance store is absent")
            with file_result.require() as legacy:
                if legacy.identity.kind != "file" or legacy.identity.link_count != 1:
                    raise StateMigrationOfflineRequired("legacy governance store is not one independent regular file")
                yield filesystem, parent, legacy


def _require_legacy_sqlite_sidecars_absent(
    filesystem: held_fs.HeldFilesystem,
    parent: held_fs.HeldDirectory,
) -> None:
    """Refuse a main-file snapshot when SQLite may have committed WAL bytes."""

    for leaf in (
        ".governance.sqlite-wal",
        ".governance.sqlite-shm",
        ".governance.sqlite-journal",
    ):
        sidecar = filesystem.file(parent, leaf, access="read")
        if sidecar.ok:
            sidecar.require().close()
            raise StateMigrationOfflineRequired("legacy governance SQLite sidecars require recovery")
        if sidecar.error is None or sidecar.error.code != "MISSING":
            raise StateMigrationOfflineRequired("legacy governance SQLite sidecars cannot be inspected")


def _same_file_identity(
    first: held_fs.StableIdentity,
    second: held_fs.StableIdentity,
) -> bool:
    return (
        first.device == second.device
        and first.inode == second.inode
        and first.kind == second.kind == "file"
    )


def _held_v3_snapshot(
    filesystem: held_fs.HeldFilesystem,
    legacy: held_fs.HeldFile,
) -> tuple[bytes, str]:
    from .governance import schema_v4, store

    read = filesystem.read(legacy)
    if not read.ok:
        raise StateMigrationOfflineRequired("legacy governance store cannot be read")
    snapshot = read.require()
    connection = sqlite3.connect(":memory:")
    try:
        connection.deserialize(snapshot)
        schema_v4.require_exact_v3_connection(connection)
        return snapshot, store._v3_snapshot_digest(connection)  # noqa: SLF001 - canonical v3 digest
    except (sqlite3.Error, schema_v4.SchemaV4Error, ValueError) as error:
        raise StateMigrationOfflineRequired("legacy governance store is not an exact v3 database") from error
    finally:
        connection.close()


def _verify_retained_legacy_snapshot(
    filesystem: held_fs.HeldFilesystem,
    parent: held_fs.HeldDirectory,
    expected_identity: held_fs.StableIdentity,
    expected_digest: str,
) -> None:
    reopened = filesystem.file(parent, ".governance.sqlite", access="read")
    if not reopened.ok:
        raise StateMigrationOfflineRequired("legacy governance store disappeared during adoption")
    with reopened.require() as current:
        if (
            not _same_file_identity(current.identity, expected_identity)
            or current.identity.link_count != 1
        ):
            raise StateMigrationOfflineRequired("legacy governance store identity changed during adoption")
        _snapshot, digest = _held_v3_snapshot(filesystem, current)
        if digest != expected_digest:
            raise StateMigrationOfflineRequired("legacy governance bytes changed during adoption")


def _replace_external_governance_from_legacy(
    vault_root: Path,
    *,
    snapshot_bytes: bytes,
    expected_digest: str,
) -> None:
    """Replace only the external governance family through one held transaction."""

    from . import reserved_paths
    from .governance import schema_migration, schema_v4, store

    root = Path(vault_root)
    external_path = store.sidecar_path(root)
    try:
        if not (
            reserved_paths.owner_authorized("governance-store")
            and reserved_paths._identity_coordination_active(root, "governance-store")  # noqa: SLF001
        ):
            raise RuntimeError("governance-store adoption lacks held identity coordination")
        with reserved_paths._sqlite_owner_target_scope(
            root,
            external_path,
            "governance-store",
            create=False,
        ) as retained_path:
            source = sqlite3.connect(":memory:")
            destination = sqlite3.connect(
                f"{retained_path.as_uri()}?mode=rw", uri=True
            )
            try:
                source.deserialize(snapshot_bytes)
                schema_v4.require_exact_v3_connection(source)
                if store._v3_snapshot_digest(source) != expected_digest:  # noqa: SLF001
                    raise StateMigrationOfflineRequired("legacy governance bytes changed during adoption")
                destination.execute("PRAGMA synchronous=FULL")
                destination.execute("BEGIN EXCLUSIVE")
                try:
                    schema_migration._replace_database_schema(destination, source)  # noqa: SLF001
                    schema_v4.require_exact_v3_connection(destination)
                    if store.canonical_uncommitted_v3_digest(destination) != expected_digest:
                        raise StateMigrationOfflineRequired("external governance copy digest differs")
                    destination.commit()
                except BaseException:
                    destination.rollback()
                    raise
                reserved_paths._publish_sqlite_owner_family(
                    root, external_path, "governance-store", destination
                )
            finally:
                destination.close()
                source.close()
    except StateMigrationOfflineRequired:
        raise
    except (OSError, RuntimeError, sqlite3.Error, schema_v4.SchemaV4Error) as error:
        raise StateMigrationOfflineRequired("governance-store adoption copy is unavailable") from error
    if _exact_v3_digest(external_path, detail="external governance store") != expected_digest:
        raise StateMigrationOfflineRequired("external governance copy proof differs")


def _remove_legacy_governance_family(
    vault_root: Path,
    *,
    expected_identity: held_fs.StableIdentity,
) -> None:
    root = Path(vault_root)
    acquired = held_fs.acquire(root)
    if not acquired.ok:
        raise StateMigrationOfflineRequired("legacy governance family cannot be retained")
    with acquired.require() as filesystem:
        parent_result = filesystem.parent(kb_dirname(), access="flush")
        if not parent_result.ok:
            raise StateMigrationOfflineRequired("legacy governance parent cannot be retained")
        with parent_result.require() as parent:
            current_result = filesystem.file(parent, ".governance.sqlite", access="mutate")
            if not current_result.ok:
                raise StateMigrationOfflineRequired("legacy governance store disappeared before removal")
            with current_result.require() as current:
                if (
                    not _same_file_identity(current.identity, expected_identity)
                    or current.identity.link_count != 1
                ):
                    raise StateMigrationOfflineRequired("legacy governance store identity changed before removal")
                removed = filesystem.unlink(current)
                if not removed.ok:
                    raise StateMigrationOfflineRequired("legacy governance store cannot be removed")
            flushed = filesystem.flush_directory(parent)
            if not flushed.ok:
                raise StateMigrationOfflineRequired("legacy governance parent cannot be flushed")
        for leaf in (
            ".governance.sqlite-wal",
            ".governance.sqlite-shm",
            ".governance.sqlite-journal",
        ):
            try:
                _adopt_remove_file(filesystem, leaf)
            except OSError as error:
                raise StateMigrationOfflineRequired("legacy governance sidecars cannot be removed") from error


def _remove_legacy_governance_residue(vault_root: Path) -> None:
    """Finish only the sidecars left by a proved main-file unlink crash."""

    root = Path(vault_root)
    acquired = held_fs.acquire(root)
    if not acquired.ok:
        raise StateMigrationOfflineRequired("legacy governance residue cannot be retained")
    with acquired.require() as filesystem:
        parent_result = filesystem.parent(kb_dirname(), access="flush")
        if not parent_result.ok:
            raise StateMigrationOfflineRequired("legacy governance parent cannot be retained")
        with parent_result.require() as parent:
            main = filesystem.file(parent, ".governance.sqlite", access="read")
            if main.ok:
                main.require().close()
                raise StateMigrationOfflineRequired("legacy governance main file unexpectedly remains")
            if main.error is None or main.error.code != "MISSING":
                raise StateMigrationOfflineRequired("legacy governance residue cannot be inspected")
        for leaf in (
            ".governance.sqlite-wal",
            ".governance.sqlite-shm",
            ".governance.sqlite-journal",
        ):
            try:
                _adopt_remove_file(filesystem, leaf)
            except OSError as error:
                raise StateMigrationOfflineRequired("legacy governance residue cannot be removed") from error


def _legacy_governance_family_present(vault_root: Path) -> bool:
    kb = Path(vault_root) / kb_dirname()
    for leaf in (
        ".governance.sqlite",
        ".governance.sqlite-wal",
        ".governance.sqlite-shm",
        ".governance.sqlite-journal",
    ):
        try:
            (kb / leaf).lstat()
        except FileNotFoundError:
            continue
        except OSError as error:
            raise StateMigrationOfflineRequired("legacy governance family cannot be inspected") from error
        return True
    return False


def _governance_adoption_replay_action(
    *,
    phase: str,
    external_digest: str,
    legacy_digest: str | None,
    legacy_residue: bool = False,
    d1_digest: str,
    adopted_digest: str,
) -> str:
    """Classify only the four crash-safe adoption replay states."""

    if phase == "prepared":
        if legacy_digest != adopted_digest:
            raise StateMigrationOfflineRequired("governance adoption prepared evidence changed")
        if external_digest == d1_digest:
            return "copy"
        if external_digest == adopted_digest:
            return "mark-copied"
    elif phase == "copied" and external_digest == adopted_digest:
        if legacy_digest == adopted_digest:
            return "remove"
        if legacy_digest is None:
            return "remove-residue" if legacy_residue else "clear"
    raise StateMigrationOfflineRequired("governance adoption replay evidence is invalid")


def _governance_adoption_barrier(_point: str) -> None:
    """Crash-injection seam between durable adoption effects."""


def _adopt_governance_store_from_vault_offline(vault_root: Path) -> StateResolution:
    from .governance import receipts

    try:
        return _adopt_governance_store_from_vault_offline_impl(vault_root)
    except receipts.ReceiptError as error:
        raise StateMigrationOfflineRequired("legacy governance receipt evidence is invalid") from error


def _adopt_governance_store_from_vault_offline_impl(vault_root: Path) -> StateResolution:
    """Re-externalize only the post-fence v3 governance authority.

    This is intentionally not the old global ``--adopt-state vault`` path:
    all unrelated external descriptor families remain byte-for-byte untouched.
    """

    from . import reserved_paths
    from .governance import legacy_v3_placement, receipts, store

    root = Path(vault_root)
    state_dir = state_paths.ensure_vault_state_dir(root)
    legacy_path = legacy_v3_placement.legacy_v3_path(root)
    external_path = store.sidecar_path(root)
    with _migration_lock(state_dir):
        with (
            receipts.exclusive_sequence(root),
            reserved_paths._subsystem_authority_scope("governance.store"),
            reserved_paths._identity_coordination_scope(
                root,
                descriptor_ids=("governance-store",),
            ),
        ):
            manifest = _load_manifest(state_dir, vault_root=root)
            if manifest is None or manifest.get("version") != 2:
                raise StateMigrationOfflineRequired("governance adoption requires a v2 rollback manifest")
            marker = manifest.get("governance_rollback")
            if not isinstance(marker, dict) or marker.get("phase") != "complete":
                raise StateMigrationOfflineRequired("governance adoption requires a completed rollback marker")
            d1 = marker.get("d1")
            endpoint = marker.get("terminal")
            if not _is_hex64(d1) or not isinstance(endpoint, Mapping):
                raise StateMigrationOfflineRequired("completed rollback marker lacks D1 evidence")
            adoption = manifest.get("governance_adoption")
            if adoption is None:
                if _exact_v3_digest(external_path, detail="external governance store") != d1:
                    raise StateMigrationOfflineRequired("external governance store is no longer the recorded D1")
                with _retained_legacy_governance_file(root) as (filesystem, parent, legacy):
                    _snapshot, legacy_digest = _held_v3_snapshot(filesystem, legacy)
                    receipts.require_legacy_receipt_descendant(root, legacy_path, endpoint)
                    _verify_retained_legacy_snapshot(
                        filesystem, parent, legacy.identity, legacy_digest
                    )
                raw = _read_manifest_bytes(_manifest_path(state_dir))
                manifest["governance_adoption"] = {
                    "phase": "prepared",
                    "event_id": marker["event_id"],
                    "d1": d1,
                    "legacy_digest": legacy_digest,
                }
                _write_manifest_cas(state_dir, manifest, expected_raw=raw)
                adoption = manifest["governance_adoption"]
            if not isinstance(adoption, dict):  # validated above; retain a fail-closed boundary
                raise StateMigrationOfflineRequired("governance adoption record is invalid")
            expected_legacy = adoption["legacy_digest"]
            phase = adoption["phase"]
            external_digest = _exact_v3_digest(external_path, detail="external governance store")
            legacy_exists = legacy_path.exists()
            legacy_residue = not legacy_exists and _legacy_governance_family_present(root)
            if legacy_exists:
                with _retained_legacy_governance_file(root) as (filesystem, parent, legacy):
                    _snapshot, legacy_digest = _held_v3_snapshot(filesystem, legacy)
                    _verify_retained_legacy_snapshot(
                        filesystem, parent, legacy.identity, legacy_digest
                    )
            else:
                legacy_digest = None
            action = _governance_adoption_replay_action(
                phase=phase,
                external_digest=external_digest,
                legacy_digest=legacy_digest,
                legacy_residue=legacy_residue,
                d1_digest=d1,
                adopted_digest=expected_legacy,
            )
            if action in {"copy", "mark-copied"}:
                if action == "copy":
                    with _retained_legacy_governance_file(root) as (filesystem, parent, legacy):
                        snapshot, digest = _held_v3_snapshot(filesystem, legacy)
                        if digest != expected_legacy:
                            raise StateMigrationOfflineRequired("legacy governance bytes changed during adoption")
                        receipts.require_legacy_receipt_descendant(root, legacy_path, endpoint)
                        _replace_external_governance_from_legacy(
                            root, snapshot_bytes=snapshot, expected_digest=expected_legacy
                        )
                        _verify_retained_legacy_snapshot(
                            filesystem, parent, legacy.identity, expected_legacy
                        )
                    _governance_adoption_barrier("after_external_copy")
                raw = _read_manifest_bytes(_manifest_path(state_dir))
                manifest["governance_adoption"]["phase"] = "copied"
                _write_manifest_cas(state_dir, manifest, expected_raw=raw)
                _governance_adoption_barrier("after_copied_marker")
                phase = "copied"
                external_digest = expected_legacy
                action = "remove"
            if action == "remove":
                with _retained_legacy_governance_file(root) as (filesystem, parent, legacy):
                    _snapshot, digest = _held_v3_snapshot(filesystem, legacy)
                    if digest != expected_legacy:
                        raise StateMigrationOfflineRequired("legacy governance store changed before removal")
                    receipts.require_legacy_receipt_descendant(root, legacy_path, endpoint)
                    _verify_retained_legacy_snapshot(
                        filesystem, parent, legacy.identity, expected_legacy
                    )
                    identity = legacy.identity
                # This is the last destructive cut: do not unlink the legacy
                # authority if a concurrent actor changed external A after the
                # prior replay classification.
                if _exact_v3_digest(external_path, detail="external governance store") != expected_legacy:
                    raise StateMigrationOfflineRequired("external governance store changed before legacy removal")
                _remove_legacy_governance_family(root, expected_identity=identity)
                if _legacy_governance_family_present(root):
                    raise StateMigrationOfflineRequired("legacy governance family removal is incomplete")
                _governance_adoption_barrier("after_legacy_removal")
            elif action == "remove-residue":
                if _exact_v3_digest(external_path, detail="external governance store") != expected_legacy:
                    raise StateMigrationOfflineRequired("external governance store changed before residue cleanup")
                _remove_legacy_governance_residue(root)
                if _legacy_governance_family_present(root):
                    raise StateMigrationOfflineRequired("legacy governance residue removal is incomplete")
                _governance_adoption_barrier("after_legacy_removal")
            elif action != "clear":  # pragma: no cover - replay classifier is closed
                raise StateMigrationOfflineRequired("governance adoption replay evidence is invalid")
            if _exact_v3_digest(external_path, detail="external governance store") != expected_legacy:
                raise StateMigrationOfflineRequired("external governance store changed before adoption completion")
            raw = _read_manifest_bytes(_manifest_path(state_dir))
            manifest.pop("governance_adoption", None)
            manifest.pop("governance_rollback", None)
            _write_manifest_cas(state_dir, manifest, expected_raw=raw)
    reset_state_resolution_cache_for_tests()
    return require_vault_state_ready(root)


def _adopt_remove_file(filesystem: held_fs.HeldFilesystem, leaf: str) -> None:
    parent_result = filesystem.parent(kb_dirname(), access="flush")
    if not parent_result.ok:
        raise OSError("adopt-state cannot retain the KB directory")
    with parent_result.require() as parent:
        file_result = filesystem.file(parent, leaf, access="mutate")
        if not file_result.ok:
            if file_result.error is not None and file_result.error.code == "MISSING":
                return
            raise OSError("adopt-state cannot remove a legacy state file")
        with file_result.require() as file:
            removed = filesystem.unlink(file)
            if not removed.ok:
                raise OSError("adopt-state could not remove a legacy state file")
        flushed = filesystem.flush_directory(parent)
        if not flushed.ok:
            raise OSError("adopt-state could not flush the KB directory")


def _adopt_remove_tree(filesystem: held_fs.HeldFilesystem, tree: Path) -> None:
    relative = f"{kb_dirname()}/{tree.name}"
    parent_result = filesystem.parent(relative)
    if not parent_result.ok:
        if parent_result.error is not None and parent_result.error.code == "MISSING":
            return
        raise OSError("adopt-state cannot retain a legacy state tree")
    with parent_result.require() as directory:
        enumerated = filesystem.enumerate(directory)
        if not enumerated.ok:
            raise OSError("adopt-state cannot enumerate a legacy state tree")
        records = enumerated.require()
    for record in records:
        if record.identity.kind != "file":
            continue
        child = f"{relative}/{record.relative_path}"
        parent_text, _, leaf = child.rpartition("/")
        child_parent = filesystem.parent(parent_text, access="flush")
        if not child_parent.ok:
            raise OSError("adopt-state cannot retain a state-tree parent")
        with child_parent.require() as parent:
            file_result = filesystem.file(parent, leaf, access="mutate")
            if not file_result.ok:
                raise OSError("adopt-state cannot retain a state-tree file")
            with file_result.require() as file:
                removed = filesystem.unlink(file)
                if not removed.ok:
                    raise OSError("adopt-state could not remove a state-tree file")
            flushed = filesystem.flush_directory(parent)
            if not flushed.ok:
                raise OSError("adopt-state could not flush a state-tree parent")
    relative = f"{kb_dirname()}/{tree.name}"
    _remove_empty_tree(filesystem, relative)
