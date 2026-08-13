"""Crash-reconcilable governed deletion and recovery lifecycle.

This module is the sole owner of immutable content manifests, deletion
tombstones, staged-recovery markers, exact placement/residue classification,
and lifecycle receipt ordering.  Tier-2 command leaves remain responsible for
their existing user-facing safety checks and derived-index fan-out only.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import sqlite3
import stat
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from .. import find_corpus, index_paths, media_types, memory_refs, mutation_lock, semantic_index
from ..kbdir import kb_dirname
from ..vault import parse_frontmatter
from . import membership, receipts
from . import policy as policy_module

SCHEMA = "governance-lifecycle/v1"
TOMBSTONE_DIR = "deletion-tombstones"
RECOVERY_DIR = "recovery"
FAIL_CLOSED_TOMBSTONE = "__governance_lifecycle_state_unavailable__"
MAX_MARKER_BYTES = 256 * 1024


@dataclass
class LifecycleError(Exception):
    code: str
    reason: str

    def __str__(self) -> str:
        return f"{self.code}: {self.reason}"


@dataclass(frozen=True)
class ManifestItem:
    source_path: str
    trash_path: str
    content_hash: str
    size: int
    kind: str
    affected_ref: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_path": self.source_path,
            "trash_path": self.trash_path,
            "content_hash": self.content_hash,
            "size": self.size,
            "kind": self.kind,
            "affected_ref": self.affected_ref,
        }


@dataclass
class LifecycleOperation:
    vault_root: Path
    operation: str
    governed: bool
    event_id: str | None
    operation_nonce: str
    manifest: tuple[ManifestItem, ...]
    source_root: str
    trash_root: str
    residue_paths: tuple[str, ...]
    manifest_digest: str
    prior_digest: str
    target_digest: str
    marker_path: Path | None
    tombstone_path: Path | None


def _checkpoint(_point: str) -> None:
    """Crash-injection seam; production is deliberately a no-op."""


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _governance_root(vault_root: Path) -> Path:
    return Path(vault_root) / kb_dirname() / policy_module.GOVERNANCE_DIRNAME


def _tombstone_root(vault_root: Path) -> Path:
    return _governance_root(vault_root) / TOMBSTONE_DIR


def _recovery_root(vault_root: Path) -> Path:
    return _tombstone_root(vault_root) / RECOVERY_DIR


def _validated_operational_directory(
    vault_root: Path, directory: Path, *, create: bool
) -> Path:
    """Return a real, in-vault lifecycle directory without following symlinks."""
    lexical_vault = Path(vault_root).absolute()
    lexical_directory = Path(directory).absolute()
    try:
        relative = lexical_directory.relative_to(lexical_vault)
    except ValueError as exc:
        raise LifecycleError(
            "LIFECYCLE_PATH_UNSAFE", "lifecycle operational path escaped the vault"
        ) from exc
    current = Path(vault_root).resolve()
    for index, part in enumerate(relative.parts):
        current = current / part
        try:
            entry = os.lstat(current)
        except FileNotFoundError:
            if not create:
                return current.joinpath(*relative.parts[index + 1 :])
            try:
                current.mkdir()
                entry = os.lstat(current)
            except OSError as exc:
                raise LifecycleError(
                    "LIFECYCLE_PATH_UNSAFE",
                    "lifecycle operational directory could not be created safely",
                ) from exc
        except OSError as exc:
            raise LifecycleError(
                "LIFECYCLE_PATH_UNSAFE",
                "lifecycle operational directory could not be inspected",
            ) from exc
        if stat.S_ISLNK(entry.st_mode) or not stat.S_ISDIR(entry.st_mode):
            raise LifecycleError(
                "LIFECYCLE_PATH_UNSAFE",
                "lifecycle operational path is not a real directory",
            )
    try:
        current.resolve().relative_to(Path(vault_root).resolve())
    except (OSError, ValueError) as exc:
        raise LifecycleError(
            "LIFECYCLE_PATH_UNSAFE", "lifecycle operational path escaped the vault"
        ) from exc
    return current


def _normalize_rel(value: str) -> str:
    return unquote(str(value or "")).replace("\\", "/").strip().strip("/")


def protected_path(vault_root: Path, rel_path: str) -> bool:
    """Whether moving ``rel_path`` would move receipt/tombstone evidence."""
    target = (Path(vault_root) / _normalize_rel(rel_path)).resolve()
    governed_root = _governance_root(vault_root).resolve()
    kb_root = (Path(vault_root) / kb_dirname()).resolve()
    try:
        target.relative_to(governed_root)
        return True
    except ValueError:
        pass
    # Protect every ancestor that contains governed operational state, including
    # the configured governed root itself.
    return target == kb_root or target in governed_root.parents


def assert_not_protected(vault_root: Path, rel_path: str) -> None:
    if protected_path(vault_root, rel_path):
        raise LifecycleError(
            "GOVERNANCE_STATE_PROTECTED",
            "governance receipts, tombstones, and every containing ancestor are not lifecycle targets",
        )


def _memory_ref(raw: bytes, rel_path: str) -> str:
    if rel_path.lower().endswith(".md"):
        try:
            frontmatter, _body, _span = parse_frontmatter(raw.decode("utf-8"))
        except (UnicodeError, ValueError):
            frontmatter = {}
        if frontmatter:
            candidate = frontmatter.get("exomem_id")
            if isinstance(candidate, str):
                try:
                    return memory_refs.memory_ref(candidate)
                except Exception:  # noqa: BLE001 - fallback is content identity
                    pass
    return f"sha256:{hashlib.sha256(raw).hexdigest()}"


def _capture_manifest(
    vault_root: Path,
    source_rel: str,
    trash_rel: str,
) -> tuple[ManifestItem, ...]:
    source = Path(vault_root) / source_rel
    if source.is_file():
        paths = [source]
    elif source.is_dir():
        paths = sorted(
            (item for item in source.rglob("*") if item.is_file()),
            key=lambda item: item.relative_to(source).as_posix(),
        )
    else:
        raise LifecycleError("LIFECYCLE_SOURCE_MISSING", "lifecycle source is not a regular file or directory")
    items: list[ManifestItem] = []
    for path in paths:
        raw = path.read_bytes()
        rel = path.relative_to(vault_root).as_posix()
        if source.is_dir():
            destination = f"{trash_rel.rstrip('/')}/{path.relative_to(source).as_posix()}"
        else:
            destination = trash_rel
        digest = hashlib.sha256(raw).hexdigest()
        items.append(
            ManifestItem(
                source_path=rel,
                trash_path=destination,
                content_hash=digest,
                size=len(raw),
                kind="file",
                affected_ref=_memory_ref(raw, rel),
            )
        )
    return tuple(items)


def _derived_residue_paths(vault_root: Path, source_rel: str) -> tuple[str, ...]:
    """Content-free derived paths whose searchable residue belongs to a source."""
    if media_types.media_type_for(source_rel) != "video":
        return ()
    from .. import scene_frames

    return tuple(
        sorted(
            scene_frames.list_scene_frame_children(
                Path(vault_root), Path(vault_root) / source_rel
            )
        )
    )


def _scope_ids(vault_root: Path, item: ManifestItem, policy: policy_module.Policy) -> frozenset[str] | None:
    source = Path(vault_root) / item.source_path
    if not item.source_path.lower().endswith(".md"):
        return membership.evaluate_path_only(
            vault_root, item.source_path, policy
        ).require_classified()
    try:
        raw = source.read_bytes()
        stat = source.stat()
        page = find_corpus.parse_page(source, stat.st_mtime, vault_root, content=raw)
    except (OSError, UnicodeError, ValueError):
        return None
    if page is None:
        return None
    return membership.evaluate_snapshot(page, policy, content_hash=item.content_hash)


def _restricting_scope_ids(policy: policy_module.Policy) -> set[str]:
    """Every scope whose membership makes an item governed material.

    A scope restricts either because a rule lowers its ceiling for someone, or
    because it DECLARES that audiences it does not name receive nothing — the
    declaration names no rule, so enumerating rules alone leaves a
    declaration-only item looking ungoverned. That misclassification is a
    disclosure bug, not a bookkeeping one: an ungoverned deletion writes no
    tombstone, so `is_tombstoned` stays False and the path can no longer be
    withheld once the item itself is gone.
    """
    return {
        scope_id
        for rule in policy.rules
        if rule.ceiling < policy_module.DISCLOSURE_MAX
        for scope_id in rule.scope_ids
    } | {
        scope_id for scope_id, scope in policy.scopes.items() if scope.default_deny
    }


def _is_governed(vault_root: Path, manifest: tuple[ManifestItem, ...]) -> bool:
    policy = policy_module.load(vault_root)
    if policy.blocked:
        return True
    if policy.empty:
        return False
    restricting_scope_ids = _restricting_scope_ids(policy)
    for item in manifest:
        try:
            scope_ids = _scope_ids(vault_root, item, policy)
        except membership.MembershipUnresolved:
            return True
        if scope_ids is None or restricting_scope_ids.intersection(scope_ids):
            return True
    return False


def _manifest_payload(manifest: tuple[ManifestItem, ...]) -> list[dict[str, Any]]:
    return [item.as_dict() for item in manifest]


def _manifest_descriptor(
    manifest: tuple[ManifestItem, ...], residue_paths: tuple[str, ...]
) -> dict[str, Any]:
    return {
        "items": _manifest_payload(manifest),
        "residue_paths": list(residue_paths),
    }


def _placement_descriptor(manifest: tuple[ManifestItem, ...], state: str) -> dict[str, Any]:
    return {
        "state": state,
        "items": [
            {
                "source_path": item.source_path,
                "trash_path": item.trash_path,
                "content_hash": item.content_hash,
            }
            for item in manifest
        ],
    }


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        try:
            mutation_lock._windows_flush_directory(path)
        except OSError as exc:
            raise LifecycleError(
                "LIFECYCLE_PATH_UNSAFE", "lifecycle durable directory fsync failed"
            ) from exc
        return
    try:
        entry = os.lstat(path)
    except OSError as exc:
        raise LifecycleError(
            "LIFECYCLE_PATH_UNSAFE", "lifecycle directory could not be inspected"
        ) from exc
    if stat.S_ISLNK(entry.st_mode) or not stat.S_ISDIR(entry.st_mode):
        raise LifecycleError(
            "LIFECYCLE_PATH_UNSAFE", "lifecycle directory is not a real directory"
        )
    fd = os.open(
        path,
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        if not os.path.samestat(entry, os.fstat(fd)):
            raise LifecycleError(
                "LIFECYCLE_PATH_UNSAFE", "lifecycle directory changed during open"
            )
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_durable_json(
    vault_root: Path, path: Path, payload: Mapping[str, Any]
) -> None:
    _validated_operational_directory(vault_root, path.parent, create=True)
    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    encoded = _canonical(dict(payload)) + b"\n"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    fd = os.open(temporary, flags, 0o600)
    try:
        with os.fdopen(fd, "wb", closefd=False) as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        os.close(fd)
    os.replace(temporary, path)
    _fsync_directory(path.parent)


def _unlink_durable(vault_root: Path, path: Path) -> None:
    _validated_operational_directory(vault_root, path.parent, create=False)
    try:
        path.unlink()
    except FileNotFoundError:
        return
    _fsync_directory(path.parent)


def _marker_payload(operation: LifecycleOperation, *, state: str) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "operation": operation.operation,
        "state": state,
        "event_id": operation.event_id,
        "operation_nonce": operation.operation_nonce,
        "source_root": operation.source_root,
        "trash_root": operation.trash_root,
        "residue_paths": list(operation.residue_paths),
        "manifest_digest": operation.manifest_digest,
        "prior_digest": operation.prior_digest,
        "target_digest": operation.target_digest,
        "manifest": _manifest_payload(operation.manifest),
    }


def _unresolved_identity(
    vault_root: Path,
    *,
    operation: str,
    manifest_digest: str,
    prior_digest: str,
    target_digest: str,
) -> tuple[str, str] | None:
    records = receipts.event_records(vault_root)
    terminals = {
        str(item.get("causation_id"))
        for item in records
        if item.get("phase") in {"committed", "aborted"}
    }
    for item in reversed(records):
        event_id = str(item.get("event_id") or "")
        affected = item.get("affected_ids")
        if (
            item.get("event_type") == "critical"
            and item.get("phase") == "intent"
            and item.get("operation") == operation
            and item.get("prepared") == manifest_digest
            and item.get("prior") == prior_digest
            and item.get("target") == target_digest
            and event_id not in terminals
            and isinstance(affected, list)
            and len(affected) == 1
            and isinstance(affected[0], str)
        ):
            return event_id, affected[0]
    return None


def begin_deletion(vault_root: Path, *, source_rel: str, trash_rel: str) -> LifecycleOperation:
    assert_not_protected(vault_root, source_rel)
    _validated_operational_directory(
        vault_root, _tombstone_root(vault_root), create=False
    )
    manifest = _capture_manifest(vault_root, source_rel, trash_rel)
    residue_paths = _derived_residue_paths(vault_root, source_rel)
    governed = _is_governed(vault_root, manifest)
    if governed and len(manifest) > receipts.MAX_OUTCOMES:
        raise LifecycleError(
            "GOVERNED_BATCH_LIMIT",
            f"governed deletion has {len(manifest)} items; receipt limit is {receipts.MAX_OUTCOMES}",
        )
    if governed:
        _validated_operational_directory(
            vault_root, _tombstone_root(vault_root), create=True
        )
    manifest_digest = _digest(_manifest_descriptor(manifest, residue_paths))
    prior = _digest(_placement_descriptor(manifest, "deletion_prior"))
    target = _digest(_placement_descriptor(manifest, "deletion_target"))
    reused = _unresolved_identity(
        vault_root,
        operation="governed_delete",
        manifest_digest=manifest_digest,
        prior_digest=prior,
        target_digest=target,
    ) if governed else None
    nonce = reused[1] if reused is not None else uuid.uuid4().hex
    if not governed:
        return LifecycleOperation(
            Path(vault_root),
            "deletion",
            False,
            None,
            nonce,
            manifest,
            source_rel,
            trash_rel,
            residue_paths,
            manifest_digest,
            prior,
            target,
            None,
            None,
        )
    event_id = (
        reused[0]
        if reused is not None
        else receipts.critical_event_id(
            {"operation": "governed_delete", "operation_nonce": nonce, "manifest_digest": manifest_digest}
        )
    )
    receipts.begin_event(
        vault_root,
        operation="governed_delete",
        prior=prior,
        target=target,
        affected_ids=[nonce],
        event_id=event_id,
        prepared=manifest_digest,
    )
    _checkpoint("deletion_intent")
    tombstone = _tombstone_root(vault_root) / f"{event_id}.json"
    operation = LifecycleOperation(
        Path(vault_root),
        "deletion",
        True,
        event_id,
        nonce,
        manifest,
        source_rel,
        trash_rel,
        residue_paths,
        manifest_digest,
        prior,
        target,
        tombstone,
        tombstone,
    )
    _write_durable_json(
        vault_root, tombstone, _marker_payload(operation, state="pending")
    )
    _checkpoint("deletion_tombstone")
    return operation


def _device(path: Path) -> int:
    return path.stat().st_dev


def _manifest_matches_source(
    vault_root: Path,
    source_root: Path,
    manifest: tuple[ManifestItem, ...],
) -> bool:
    expected = {item.source_path: (item.content_hash, item.size) for item in manifest}
    if source_root.is_file():
        actual_paths = [source_root]
    elif source_root.is_dir():
        actual_paths = sorted(path for path in source_root.rglob("*") if path.is_file())
    else:
        return False
    actual: dict[str, tuple[str, int]] = {}
    for path in actual_paths:
        try:
            raw = path.read_bytes()
            rel = path.relative_to(vault_root).as_posix()
        except (OSError, ValueError):
            return False
        actual[rel] = (hashlib.sha256(raw).hexdigest(), len(raw))
    return actual == expected


def atomic_rename(
    operation: LifecycleOperation,
    *, source: Path,
    destination: Path,
    recovery: bool = False,
) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        source_device = _device(source)
        destination_device = _device(destination.parent)
    except OSError as exc:
        raise LifecycleError("ATOMIC_MOVE_PREFLIGHT_FAILED", "could not verify lifecycle move device") from exc
    if source_device != destination_device:
        raise LifecycleError("CROSS_DEVICE_MOVE", "lifecycle moves require a same-device atomic rename")
    if destination.exists():
        raise LifecycleError("ATOMIC_MOVE_DEST_EXISTS", "atomic lifecycle destination already exists")
    expected = tuple(
        ManifestItem(
            source_path=item.trash_path if recovery else item.source_path,
            trash_path=item.source_path if recovery else item.trash_path,
            content_hash=item.content_hash,
            size=item.size,
            kind=item.kind,
            affected_ref=item.affected_ref,
        )
        for item in operation.manifest
    )
    if not _manifest_matches_source(operation.vault_root, source, expected):
        raise LifecycleError("LIFECYCLE_CENSUS_DRIFT", "lifecycle source changed after manifest capture")
    try:
        os.rename(source, destination)
    except OSError as exc:
        code = "CROSS_DEVICE_MOVE" if exc.errno == errno.EXDEV else "ATOMIC_MOVE_FAILED"
        raise LifecycleError(code, "atomic lifecycle rename failed") from exc
    _fsync_directory(source.parent)
    if destination.parent != source.parent:
        _fsync_directory(destination.parent)
    _checkpoint("recovery_moved" if recovery else "deletion_moved")


def _read_json(vault_root: Path, path: Path) -> dict[str, Any] | None:
    _validated_operational_directory(vault_root, path.parent, create=False)
    try:
        entry = os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise LifecycleError(
            "LIFECYCLE_PATH_UNSAFE", "lifecycle marker could not be inspected"
        ) from exc
    if stat.S_ISLNK(entry.st_mode) or not stat.S_ISREG(entry.st_mode):
        raise LifecycleError(
            "LIFECYCLE_PATH_UNSAFE", "lifecycle marker is not a regular file"
        )
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as exc:
        raise LifecycleError(
            "LIFECYCLE_PATH_UNSAFE", "lifecycle marker could not be opened safely"
        ) from exc
    try:
        opened = os.fstat(fd)
        if not os.path.samestat(entry, opened) or opened.st_size > MAX_MARKER_BYTES:
            raise LifecycleError(
                "LIFECYCLE_PATH_UNSAFE", "lifecycle marker changed or is oversized"
            )
        raw = os.read(fd, MAX_MARKER_BYTES + 1)
    finally:
        os.close(fd)
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError):
        return None
    return value if isinstance(value, dict) and value.get("schema") == SCHEMA else None


def _marker_shape_valid(
    marker: Mapping[str, Any],
    allowed: frozenset[str],
    records: list[dict[str, Any]],
) -> bool:
    operation = marker.get("operation")
    state = marker.get("state")
    allowed_states = {
        "deletion": {"pending", "committed"},
        "recovery_guard": {"active"},
        "recovery": {"staged"},
    }
    manifest = marker.get("manifest")
    residue_paths = marker.get("residue_paths")
    if (
        operation not in allowed
        or state not in allowed_states.get(str(operation), set())
        or not isinstance(manifest, list)
        or not isinstance(residue_paths, list)
        or not all(isinstance(value, str) for value in residue_paths)
    ):
        return False
    required_strings = (
        "event_id",
        "operation_nonce",
        "source_root",
        "trash_root",
        "manifest_digest",
        "prior_digest",
        "target_digest",
    )
    if not all(isinstance(marker.get(field), str) and marker.get(field) for field in required_strings):
        return False
    try:
        items = tuple(ManifestItem(**item) for item in manifest)
    except (TypeError, ValueError):
        return False
    residue = tuple(str(value) for value in residue_paths)
    manifest_digest = _digest(_manifest_descriptor(items, residue))
    lifecycle_kind = "deletion" if operation == "deletion" else "recovery"
    prior = _digest(_placement_descriptor(items, f"{lifecycle_kind}_prior"))
    target = _digest(_placement_descriptor(items, f"{lifecycle_kind}_target"))
    if (
        marker.get("manifest_digest") != manifest_digest
        or marker.get("prior_digest") != prior
        or marker.get("target_digest") != target
    ):
        return False
    intent_operation = (
        "governed_delete" if operation == "deletion" else "governed_recovery"
    )
    return any(
        record.get("event_type") == "critical"
        and record.get("phase") == "intent"
        and record.get("event_id") == marker.get("event_id")
        and record.get("operation") == intent_operation
        and record.get("prepared") == manifest_digest
        and record.get("prior") == prior
        and record.get("target") == target
        and record.get("affected_ids") == [marker.get("operation_nonce")]
        for record in records
    )


def _lifecycle_state_unavailable(vault_root: Path) -> bool:
    roots = (
        (_tombstone_root(vault_root), frozenset({"deletion", "recovery_guard"})),
        (_recovery_root(vault_root), frozenset({"recovery"})),
    )
    marker_sets: list[tuple[list[Path], frozenset[str]]] = []
    for candidate, allowed in roots:
        try:
            root = _validated_operational_directory(
                vault_root, candidate, create=False
            )
            paths = sorted(root.glob("*.json")) if root.exists() else []
        except (LifecycleError, OSError):
            return True
        marker_sets.append((paths, allowed))
    if not any(paths for paths, _allowed in marker_sets):
        return False
    try:
        records = receipts.event_records(vault_root)
    except Exception:  # noqa: BLE001 - unverifiable guard evidence must fail closed
        return True
    for paths, allowed in marker_sets:
        for path in paths:
            try:
                marker = _read_json(vault_root, path)
            except LifecycleError:
                return True
            if marker is None or not _marker_shape_valid(marker, allowed, records):
                return True
    return False


def _operation_from_marker(vault_root: Path, path: Path, marker: Mapping[str, Any]) -> LifecycleOperation | None:
    try:
        manifest = tuple(ManifestItem(**item) for item in marker["manifest"])
        return LifecycleOperation(
            Path(vault_root),
            str(marker["operation"]),
            True,
            str(marker["event_id"]),
            str(marker["operation_nonce"]),
            manifest,
            str(marker["source_root"]),
            str(marker["trash_root"]),
            tuple(str(value) for value in marker.get("residue_paths", [])),
            str(marker["manifest_digest"]),
            str(marker["prior_digest"]),
            str(marker["target_digest"]),
            path,
            path if marker["operation"] == "deletion" else _find_tombstone(vault_root, manifest),
        )
    except (KeyError, TypeError, ValueError):
        return None


def _query_path(path: Path, queries: tuple[tuple[str, tuple[str, ...]], ...]) -> bool | None:
    if not path.exists():
        return False
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            for sql, values in queries:
                for value in values:
                    if conn.execute(sql, (value,)).fetchone() is not None:
                        return True
        finally:
            conn.close()
    except sqlite3.Error:
        return None
    return False


def _all_rows(path: Path, sql: str, values: tuple[str, ...]) -> bool | None:
    if not values:
        return True
    if not path.exists():
        return False
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            return all(conn.execute(sql, (value,)).fetchone() is not None for value in values)
        finally:
            conn.close()
    except sqlite3.Error:
        return None


def direct_residue(
    vault_root: Path,
    manifest: tuple[ManifestItem, ...],
    residue_paths: tuple[str, ...] = (),
) -> dict[str, bool | None]:
    """Probe each searchable sidecar directly; ``None`` means unverifiable."""
    rels = tuple(
        dict.fromkeys([*(item.source_path for item in manifest), *residue_paths])
    )
    lexical = _query_path(
        Path(vault_root) / kb_dirname() / ".lexical.sqlite",
        (
            ("SELECT 1 FROM pages WHERE path = ? LIMIT 1", rels),
            ("SELECT 1 FROM semantic_units WHERE parent_path = ? LIMIT 1", rels),
        ),
    )
    refs = _query_path(
        Path(vault_root) / kb_dirname() / ".refs.sqlite",
        (("SELECT 1 FROM identities WHERE path = ? LIMIT 1", rels),),
    )
    graph = _query_path(
        Path(vault_root) / kb_dirname() / ".graph.sqlite",
        (
            ("SELECT 1 FROM graph_nodes WHERE path = ? LIMIT 1", rels),
            ("SELECT 1 FROM graph_parent_refs WHERE path = ? LIMIT 1", rels),
            ("SELECT 1 FROM graph_edges WHERE source_path = ? LIMIT 1", rels),
        ),
    )
    embeddings = _query_path(
        index_paths.sidecar_path(Path(vault_root)),
        (
            ("SELECT 1 FROM chunks WHERE file_path = ? LIMIT 1", rels),
            ("SELECT 1 FROM semantic_unit_vectors WHERE parent_path = ? LIMIT 1", rels),
        ),
    )
    clip = _query_path(
        index_paths.clip_sidecar_path(Path(vault_root)),
        (("SELECT 1 FROM images WHERE file_path = ? LIMIT 1", rels),),
    )
    scene = False
    for rel in rels:
        path = Path(vault_root) / rel
        frame_dir = path.parent / f"{path.name}.frames"
        if frame_dir.exists() and any(frame_dir.rglob("*")):
            scene = True
            break
    return {
        "lexical": lexical,
        "refs": refs,
        "graph": graph,
        "embeddings": embeddings,
        "semantic_units": lexical if lexical is None else bool(lexical),
        "clip": clip,
        "scene": scene,
    }


def _placement(operation: LifecycleOperation) -> str:
    source_root = operation.vault_root / operation.source_root
    trash_root = operation.vault_root / operation.trash_root
    source_matches = _manifest_matches_source(
        operation.vault_root,
        source_root,
        operation.manifest,
    )
    trash_manifest = tuple(
        ManifestItem(
            source_path=item.trash_path,
            trash_path=item.source_path,
            content_hash=item.content_hash,
            size=item.size,
            kind=item.kind,
            affected_ref=item.affected_ref,
        )
        for item in operation.manifest
    )
    trash_matches = _manifest_matches_source(
        operation.vault_root,
        trash_root,
        trash_manifest,
    )
    if source_matches and not trash_matches:
        return "source"
    if trash_matches and not source_matches:
        return "trash"
    return "ambiguous"


def classify_deletion(operation: LifecycleOperation) -> str | None:
    placement = _placement(operation)
    if placement == "source":
        return operation.prior_digest
    if placement != "trash":
        return None
    residue = direct_residue(
        operation.vault_root,
        operation.manifest,
        operation.residue_paths,
    )
    if any(value is None or value is True for value in residue.values()):
        return None
    return operation.target_digest


def _lifecycle_payload(operation: LifecycleOperation, exact_state_digest: str) -> dict[str, Any]:
    return {
        "manifest_digest": operation.manifest_digest,
        "affected_refs": [item.affected_ref for item in operation.manifest],
        "content_hashes": [item.content_hash for item in operation.manifest],
        "exact_state_digest": exact_state_digest,
        "causation_id": str(operation.event_id),
    }


def _record_lifecycle(operation: LifecycleOperation, event_type: str, exact_state_digest: str) -> None:
    receipts.append_event(
        operation.vault_root,
        event_type=event_type,
        payload=_lifecycle_payload(operation, exact_state_digest),
        event_id=operation.operation_nonce,
        critical=True,
    )


_CLOSED_ACCEPTED_INDEX_CODES = frozenset(
    {
        "no_eligible_paths",
        "embeddings_disabled",
        "clip_disabled",
    }
)


def _index_report_is_exact(index_report: Mapping[str, Any] | None) -> bool:
    """Whether post-transition derived state is proved before terminalizing it."""
    if index_report is None:
        return False
    if index_report.get("paths_truncated") is True or index_report.get("reconcile_required") is True:
        return False
    if index_report.get("derived_work") == "unverified":
        return False
    components = index_report.get("components")
    if not isinstance(components, (list, tuple)):
        return False
    for item in components:
        if not isinstance(item, Mapping):
            return False
        outcome = item.get("outcome")
        if outcome == "accepted":
            if item.get("code") not in _CLOSED_ACCEPTED_INDEX_CODES:
                return False
        elif outcome not in {"completed", "registered", "not_required"}:
            return False
    return True


def exact_no_derived_index_report(
    operation: LifecycleOperation,
) -> dict[str, object] | None:
    """Return explicit proof only where this lifecycle transition has no index work."""
    has_markdown = any(item.source_path.lower().endswith(".md") for item in operation.manifest)
    has_visual_media = any(
        media_types.media_type_for(item.source_path) in {"image", "video"}
        for item in operation.manifest
    )
    clip_enabled = os.environ.get("EXOMEM_DISABLE_CLIP", "").strip().lower() not in {
        "1",
        "true",
        "yes",
        "on",
    }
    if has_markdown or (has_visual_media and clip_enabled):
        return None
    return {
        "components": [],
        "derived_work": "not_required",
        "paths_truncated": False,
        "reconcile_required": False,
    }


def finish_deletion(
    operation: LifecycleOperation,
    *,
    index_report: Mapping[str, Any] | None = None,
) -> bool:
    if not operation.governed:
        return True
    if not _index_report_is_exact(index_report):
        return False
    if classify_deletion(operation) != operation.target_digest:
        return False
    _record_lifecycle(operation, "deletion", operation.target_digest)
    _checkpoint("deletion_record")
    receipts.commit_event(operation.vault_root, str(operation.event_id), outcome="deleted")
    _checkpoint("deletion_terminal")
    assert operation.tombstone_path is not None
    _write_durable_json(
        operation.vault_root,
        operation.tombstone_path,
        _marker_payload(operation, state="committed"),
    )
    return True


def abort_deletion(operation: LifecycleOperation) -> None:
    if not operation.governed:
        return
    if classify_deletion(operation) == operation.prior_digest:
        receipts.abort_event(operation.vault_root, str(operation.event_id), outcome="not_deleted")
        if operation.tombstone_path is not None:
            _unlink_durable(operation.vault_root, operation.tombstone_path)


def _iter_tombstones(vault_root: Path) -> list[tuple[Path, dict[str, Any]]]:
    root = _validated_operational_directory(
        vault_root, _tombstone_root(vault_root), create=False
    )
    if not root.exists():
        return []
    out: list[tuple[Path, dict[str, Any]]] = []
    for path in sorted(root.glob("*.json")):
        marker = _read_json(vault_root, path)
        if marker is not None and marker.get("operation") in {"deletion", "recovery_guard"}:
            out.append((path, marker))
    return out


def _iter_recovery_markers(vault_root: Path) -> list[tuple[Path, dict[str, Any]]]:
    root = _validated_operational_directory(
        vault_root, _recovery_root(vault_root), create=False
    )
    if not root.exists():
        return []
    out: list[tuple[Path, dict[str, Any]]] = []
    for path in sorted(root.glob("*.json")):
        marker = _read_json(vault_root, path)
        if marker is not None and marker.get("operation") == "recovery":
            out.append((path, marker))
    return out


def _find_tombstone(vault_root: Path, manifest: tuple[ManifestItem, ...]) -> Path | None:
    trash_paths = {item.trash_path for item in manifest}
    for path, marker in _iter_tombstones(vault_root):
        existing = {str(item.get("trash_path")) for item in marker.get("manifest", []) if isinstance(item, Mapping)}
        if existing == trash_paths:
            return path
    return None


def begin_recovery(vault_root: Path, *, trash_rel: str, source_rel: str) -> LifecycleOperation:
    assert_not_protected(vault_root, source_rel)
    tombstone_path = None
    tombstone_marker = None
    for path, marker in _iter_tombstones(vault_root):
        if any(
            str(item.get("trash_path")) == trash_rel
            or str(item.get("trash_path")).startswith(trash_rel.rstrip("/") + "/")
            for item in marker.get("manifest", [])
            if isinstance(item, Mapping)
        ):
            tombstone_path, tombstone_marker = path, marker
            break
    current_manifest = _capture_manifest(vault_root, trash_rel, source_rel)
    # Capture used the current trash path as source; invert into the deletion
    # manifest's canonical source/trash orientation.
    manifest = tuple(
        ManifestItem(item.trash_path, item.source_path, item.content_hash, item.size, item.kind, item.affected_ref)
        for item in current_manifest
    )
    lineage = (
        tombstone_marker is not None
        and tombstone_marker.get("operation") == "deletion"
        and tombstone_marker.get("state") == "committed"
    )
    governed = lineage or _is_governed_for_restore(vault_root, manifest)
    residue_paths: tuple[str, ...] = ()
    manifest_digest = _digest(_manifest_descriptor(manifest, residue_paths))
    prior = _digest(_placement_descriptor(manifest, "recovery_prior"))
    target = _digest(_placement_descriptor(manifest, "recovery_target"))
    reused = _unresolved_identity(
        vault_root,
        operation="governed_recovery",
        manifest_digest=manifest_digest,
        prior_digest=prior,
        target_digest=target,
    ) if governed else None
    nonce = reused[1] if reused is not None else uuid.uuid4().hex
    if not governed:
        return LifecycleOperation(
            Path(vault_root),
            "recovery",
            False,
            None,
            nonce,
            manifest,
            source_rel,
            trash_rel,
            residue_paths,
            manifest_digest,
            prior,
            target,
            None,
            tombstone_path,
        )
    if len(manifest) > receipts.MAX_OUTCOMES:
        raise LifecycleError("GOVERNED_BATCH_LIMIT", "governed recovery exceeds the receipt item limit")
    _validated_operational_directory(
        vault_root, _tombstone_root(vault_root), create=True
    )
    _validated_operational_directory(
        vault_root, _recovery_root(vault_root), create=True
    )
    event_id = (
        reused[0]
        if reused is not None
        else receipts.critical_event_id(
            {"operation": "governed_recovery", "operation_nonce": nonce, "manifest_digest": manifest_digest, "source": _digest(source_rel)}
        )
    )
    receipts.begin_event(
        vault_root,
        operation="governed_recovery",
        prior=prior,
        target=target,
        affected_ids=[nonce],
        event_id=event_id,
        prepared=manifest_digest,
    )
    _checkpoint("recovery_intent")
    if tombstone_path is None:
        tombstone_path = _tombstone_root(vault_root) / f"recovery-{event_id}.json"
    marker_path = _recovery_root(vault_root) / f"{event_id}.json"
    operation = LifecycleOperation(
        Path(vault_root),
        "recovery",
        True,
        event_id,
        nonce,
        manifest,
        source_rel,
        trash_rel,
        residue_paths,
        manifest_digest,
        prior,
        target,
        marker_path,
        tombstone_path,
    )
    if tombstone_marker is None:
        guard = _marker_payload(operation, state="active")
        guard["operation"] = "recovery_guard"
        _write_durable_json(vault_root, tombstone_path, guard)
    _write_durable_json(
        vault_root, marker_path, _marker_payload(operation, state="staged")
    )
    _checkpoint("recovery_marker")
    return operation


def _is_governed_for_restore(vault_root: Path, manifest: tuple[ManifestItem, ...]) -> bool:
    policy = policy_module.load(vault_root)
    if policy.blocked:
        return True
    if policy.empty:
        return False
    restricting = _restricting_scope_ids(policy)
    for item in manifest:
        if not item.source_path.lower().endswith(".md"):
            try:
                scope_ids = membership.evaluate_path_only(
                    vault_root, item.source_path, policy
                ).require_classified()
            except membership.MembershipUnresolved:
                return True
            if restricting.intersection(scope_ids):
                return True
            continue
        try:
            raw = (Path(vault_root) / item.trash_path).read_bytes()
            page = find_corpus.parse_page(
                Path(vault_root) / item.source_path,
                0.0,
                Path(vault_root),
                content=raw,
            )
        except (OSError, UnicodeError, ValueError):
            return True
        if page is None:
            return True
        scope_ids = membership.evaluate_snapshot(
            page, policy, content_hash=item.content_hash
        )
        if restricting.intersection(scope_ids):
            return True
    return False


def classify_recovery(operation: LifecycleOperation, *, require_indexes: bool = True) -> str | None:
    placement = _placement(operation)
    if placement == "trash":
        return operation.prior_digest
    if placement != "source" or operation.tombstone_path is None or not operation.tombstone_path.exists():
        return None
    if require_indexes and not _restored_derivatives_exact(operation):
        return None
    return operation.target_digest


def _restored_derivatives_exact(operation: LifecycleOperation) -> bool:
    vault_root = operation.vault_root
    markdown = tuple(
        item.source_path
        for item in operation.manifest
        if item.source_path.lower().endswith(".md")
    )
    if markdown:
        lexical_path = Path(vault_root) / kb_dirname() / ".lexical.sqlite"
        checks: list[bool | None] = [
            _all_rows(
                Path(vault_root) / kb_dirname() / ".refs.sqlite",
                "SELECT 1 FROM identities WHERE path = ? LIMIT 1",
                markdown,
            )
        ]
        if lexical_path.exists():
            checks.append(
                _all_rows(
                lexical_path,
                "SELECT 1 FROM pages WHERE path = ? LIMIT 1",
                markdown,
                )
            )
        graph_enabled = os.environ.get("EXOMEM_DISABLE_GRAPH_INDEX", "").strip().lower() not in {
            "1",
            "true",
            "yes",
            "on",
        }
        if graph_enabled:
            checks.append(
                _all_rows(
                    Path(vault_root) / kb_dirname() / ".graph.sqlite",
                    "SELECT 1 FROM graph_nodes WHERE path = ? LIMIT 1",
                    markdown,
                )
            )
        vectors_enabled = not os.environ.get("EXOMEM_DISABLE_EMBEDDINGS")
        if vectors_enabled:
            checks.append(
                _all_rows(
                    index_paths.sidecar_path(vault_root),
                    "SELECT 1 FROM chunks WHERE file_path = ? LIMIT 1",
                    markdown,
                )
            )
        if any(value is not True for value in checks):
            return False
        try:
            states = {
                rel: semantic_index.build_parent_index_state(vault_root, vault_root / rel)
                for rel in markdown
            }
            drift = semantic_index.audit_semantic_unit_sidecars(
                vault_root,
                states,
                include_lexical=lexical_path.exists(),
                include_vectors=vectors_enabled,
                include_graph=graph_enabled,
            )
        except (OSError, UnicodeError, ValueError, sqlite3.Error):
            return False
        if any(item.parent_path in states for item in drift):
            return False

    visual = tuple(
        item.source_path
        for item in operation.manifest
        if media_types.media_type_for(item.source_path) in {"image", "video"}
    )
    clip_enabled = os.environ.get("EXOMEM_DISABLE_CLIP", "").strip().lower() not in {
        "1",
        "true",
        "yes",
        "on",
    }
    if visual and clip_enabled:
        if _all_rows(
            index_paths.clip_sidecar_path(vault_root),
            "SELECT 1 FROM images WHERE file_path = ? LIMIT 1",
            visual,
        ) is not True:
            return False
    videos = tuple(rel for rel in visual if media_types.media_type_for(rel) == "video")
    if videos and clip_enabled:
        from .. import scene_frames

        if scene_frames.scene_frames_enabled() and not _scene_derivatives_exact(
            vault_root, videos
        ):
            return False
    return True


def _scene_derivatives_exact(vault_root: Path, videos: tuple[str, ...]) -> bool:
    """Prove persisted scene frames match every indexed video scene timestamp."""
    from .. import scene_frames

    clip_path = index_paths.clip_sidecar_path(vault_root)
    if not clip_path.exists():
        return False
    try:
        conn = sqlite3.connect(f"file:{clip_path}?mode=ro", uri=True)
        try:
            for rel in videos:
                rows = conn.execute(
                    "SELECT frame_ts FROM images WHERE file_path = ? ORDER BY frame_ts",
                    (rel,),
                ).fetchall()
                indexed = {
                    int(round(float(row[0]) * 1000))
                    for row in rows
                    if row[0] is not None
                }
                if not indexed or len(indexed) != len(rows):
                    return False
                frame_dir = scene_frames.frames_dir_for(Path(vault_root) / rel)
                if not frame_dir.is_dir():
                    return False
                persisted: set[int] = set()
                for frame in frame_dir.iterdir():
                    timestamp = scene_frames.parse_frame_ts(frame.name)
                    if timestamp is None:
                        continue
                    millis = int(round(timestamp * 1000))
                    sidecar = frame.with_name(frame.name + ".md")
                    try:
                        frontmatter, _body, _span = parse_frontmatter(
                            sidecar.read_text(encoding="utf-8")
                        )
                        sidecar_ts = int(
                            round(float(frontmatter.get("frame_ts")) * 1000)
                        )
                    except (OSError, UnicodeError, TypeError, ValueError):
                        return False
                    if frontmatter.get("parent_media") != rel or sidecar_ts != millis:
                        return False
                    persisted.add(millis)
                if persisted != indexed:
                    return False
        finally:
            conn.close()
    except sqlite3.Error:
        return False
    return True


def finish_recovery(operation: LifecycleOperation, *, index_report: Mapping[str, Any] | None = None) -> bool:
    if not operation.governed:
        return True
    if not _index_report_is_exact(index_report):
        return False
    if classify_recovery(operation) != operation.target_digest:
        return False
    _record_lifecycle(operation, "recovery", operation.target_digest)
    _checkpoint("recovery_record")
    receipts.commit_event(operation.vault_root, str(operation.event_id), outcome="recovered")
    _checkpoint("recovery_terminal")
    if operation.tombstone_path is not None:
        _unlink_durable(operation.vault_root, operation.tombstone_path)
    if operation.marker_path is not None:
        _unlink_durable(operation.vault_root, operation.marker_path)
    return True


def abort_recovery(operation: LifecycleOperation) -> None:
    if not operation.governed:
        return
    if classify_recovery(operation, require_indexes=False) == operation.prior_digest:
        receipts.abort_event(operation.vault_root, str(operation.event_id), outcome="not_restored")
        if operation.marker_path is not None:
            _unlink_durable(operation.vault_root, operation.marker_path)
        if operation.tombstone_path is not None:
            marker = _read_json(operation.vault_root, operation.tombstone_path)
            if marker is not None and marker.get("operation") == "recovery_guard":
                _unlink_durable(operation.vault_root, operation.tombstone_path)


def _entry_signature(path: Path) -> tuple[Any, ...]:
    try:
        entry = os.lstat(path)
    except FileNotFoundError:
        return (str(path), "missing")
    except OSError as exc:
        return (str(path), "error", type(exc).__name__)
    return (
        str(path),
        entry.st_dev,
        entry.st_ino,
        entry.st_mode,
        entry.st_size,
        entry.st_mtime_ns,
        entry.st_ctime_ns,
    )


def _tombstone_generation(vault_root: Path) -> tuple[tuple[Any, ...], ...]:
    roots = (
        _tombstone_root(vault_root),
        _recovery_root(vault_root),
        _governance_root(vault_root) / "events",
    )
    entries: list[tuple[Any, ...]] = [_entry_signature(path) for path in roots]
    tombstone_root, recovery_root, events_root = roots
    try:
        if not stat.S_ISLNK(os.lstat(tombstone_root).st_mode):
            entries.extend(
                _entry_signature(path)
                for path in sorted(tombstone_root.glob("*.json"))
            )
    except FileNotFoundError:
        pass
    except OSError as exc:
        entries.append((str(tombstone_root), "scan-error", type(exc).__name__))
    try:
        if not stat.S_ISLNK(os.lstat(recovery_root).st_mode):
            entries.extend(
                _entry_signature(path)
                for path in sorted(recovery_root.glob("*.json"))
            )
    except FileNotFoundError:
        pass
    except OSError as exc:
        entries.append((str(recovery_root), "scan-error", type(exc).__name__))
    try:
        if not stat.S_ISLNK(os.lstat(events_root).st_mode):
            entries.extend(
                _entry_signature(path)
                for path in sorted(events_root.glob("*/*.jsonl"))
            )
    except FileNotFoundError:
        pass
    except OSError as exc:
        entries.append((str(events_root), "scan-error", type(exc).__name__))
    sidecar = index_paths.governance_sidecar_path(Path(vault_root))
    entries.extend(
        _entry_signature(Path(f"{sidecar}{suffix}")) for suffix in ("", "-wal", "-shm")
    )
    return tuple(entries)


@lru_cache(maxsize=128)
def _cached_tombstoned_paths(
    vault_root: str, _generation: tuple[tuple[Any, ...], ...]
) -> frozenset[str]:
    return _compute_tombstoned_paths(Path(vault_root))


def _compute_tombstoned_paths(vault_root: Path) -> frozenset[str]:
    if _lifecycle_state_unavailable(vault_root):
        return frozenset({FAIL_CLOSED_TOMBSTONE})
    values: set[str] = set()
    markers = [*_iter_tombstones(vault_root), *_iter_recovery_markers(vault_root)]
    for _path, marker in markers:
        for item in marker.get("manifest", []):
            if not isinstance(item, Mapping):
                continue
            for field in ("source_path", "trash_path", "affected_ref"):
                value = item.get(field)
                if isinstance(value, str) and not (
                    field == "affected_ref" and value.startswith("sha256:")
                ):
                    values.add(_normalize_rel(value))
        for value in marker.get("residue_paths", []):
            if isinstance(value, str):
                values.add(_normalize_rel(value))
    return frozenset(values)


def tombstoned_paths(vault_root: Path) -> frozenset[str]:
    root = Path(vault_root).resolve()
    return _cached_tombstoned_paths(str(root), _tombstone_generation(root))


def is_tombstoned(vault_root: Path, value: str) -> bool:
    normalized = _normalize_rel(value)
    if not normalized:
        return False
    tombstones = tombstoned_paths(vault_root)
    return FAIL_CLOSED_TOMBSTONE in tombstones or normalized in tombstones


def _records(vault_root: Path) -> list[dict[str, Any]]:
    return receipts.event_records(vault_root)


def _has_terminal(records: list[dict[str, Any]], event_id: str, phase: str | None = None) -> bool:
    return any(
        item.get("causation_id") == event_id
        and item.get("phase") in ({phase} if phase else {"committed", "aborted"})
        for item in records
    )


def _has_lifecycle_record(records: list[dict[str, Any]], operation: LifecycleOperation) -> bool:
    return any(
        item.get("event_type") == operation.operation
        and item.get("causation_id") == operation.event_id
        and item.get("manifest_digest") == operation.manifest_digest
        for item in records
    )


def reconcile(vault_root: Path, *, dry_run: bool = False) -> dict[str, Any]:
    """Reconcile markers independently of unresolved critical intents."""
    repairs: list[dict[str, Any]] = []
    records = _records(vault_root)
    for path, marker in _iter_tombstones(vault_root):
        if marker.get("operation") != "deletion":
            continue
        if not _marker_shape_valid(marker, frozenset({"deletion"}), records):
            continue
        operation = _operation_from_marker(vault_root, path, marker)
        if operation is None:
            continue
        state = str(marker.get("state"))
        current = classify_deletion(operation)
        if state == "pending" and current == operation.prior_digest:
            repair = {"kind": "deletion_abort", "event_id": str(operation.event_id)}
            repairs.append(repair)
            if not dry_run:
                if not _has_terminal(records, str(operation.event_id)):
                    receipts.abort_event(vault_root, str(operation.event_id), outcome="not_deleted")
                _unlink_durable(vault_root, path)
        elif state == "pending" and current == operation.target_digest:
            repair = {"kind": "deletion_commit", "event_id": str(operation.event_id)}
            repairs.append(repair)
            if not dry_run:
                if not _has_lifecycle_record(records, operation):
                    _record_lifecycle(operation, "deletion", operation.target_digest)
                if not _has_terminal(records, str(operation.event_id), "committed"):
                    receipts.commit_event(vault_root, str(operation.event_id), outcome="deleted")
                _write_durable_json(
                    vault_root,
                    path,
                    _marker_payload(operation, state="committed"),
                )

    recovery_markers = _iter_recovery_markers(vault_root)
    if recovery_markers:
        for path, marker in recovery_markers:
            if not _marker_shape_valid(marker, frozenset({"recovery"}), records):
                continue
            operation = _operation_from_marker(vault_root, path, marker)
            if operation is None:
                continue
            current = classify_recovery(operation)
            if current == operation.prior_digest:
                repair = {"kind": "recovery_abort", "event_id": str(operation.event_id)}
                repairs.append(repair)
                if not dry_run:
                    if not _has_terminal(records, str(operation.event_id)):
                        receipts.abort_event(vault_root, str(operation.event_id), outcome="not_restored")
                    _unlink_durable(vault_root, path)
                    if operation.tombstone_path is not None:
                        tombstone = _read_json(vault_root, operation.tombstone_path)
                        if tombstone is not None and tombstone.get("operation") == "recovery_guard":
                            _unlink_durable(vault_root, operation.tombstone_path)
            elif current == operation.target_digest:
                repair = {"kind": "recovery_finalize", "event_id": str(operation.event_id)}
                repairs.append(repair)
                if not dry_run:
                    if not _has_lifecycle_record(records, operation):
                        _record_lifecycle(operation, "recovery", operation.target_digest)
                    if not _has_terminal(records, str(operation.event_id), "committed"):
                        receipts.commit_event(vault_root, str(operation.event_id), outcome="recovered")
                    if operation.tombstone_path is not None:
                        _unlink_durable(vault_root, operation.tombstone_path)
                    _unlink_durable(vault_root, path)
    return {"dry_run": dry_run, "repairs": repairs}


def _resolve_intent(vault_root: Path, intent: Mapping[str, Any]) -> str | None:
    event_id = str(intent.get("event_id") or "")
    operation_name = str(intent.get("operation") or "")
    paths: list[Path]
    if operation_name == "governed_delete":
        paths = [_tombstone_root(vault_root) / f"{event_id}.json"]
    elif operation_name == "governed_recovery":
        paths = [_recovery_root(vault_root) / f"{event_id}.json"]
    else:
        return None
    records = _records(vault_root)
    allowed = (
        frozenset({"deletion"})
        if operation_name == "governed_delete"
        else frozenset({"recovery"})
    )
    for path in paths:
        try:
            marker = _read_json(vault_root, path)
        except LifecycleError:
            return None
        if marker is None:
            # Intent is durable before marker/state mutation, so absence is an
            # exact prior under this protocol.
            return str(intent.get("prior"))
        if not _marker_shape_valid(marker, allowed, records):
            return None
        lifecycle = _operation_from_marker(vault_root, path, marker)
        if lifecycle is None:
            return None
        current = (
            classify_deletion(lifecycle)
            if operation_name == "governed_delete"
            else classify_recovery(lifecycle)
        )
        if current == lifecycle.target_digest and not _has_lifecycle_record(
            records, lifecycle
        ):
            # Lifecycle evidence is ordered before the terminal.  The marker
            # reconciler appends it exactly once, then commits the intent.
            return None
        return current
    return None


receipts.register_state_resolver("governed_delete", _resolve_intent)
receipts.register_state_resolver("governed_recovery", _resolve_intent)
