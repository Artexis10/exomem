"""Guarded, profile-neutral structured-record mutations."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import stat
import unicodedata
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import record_formats, vault, writer_lease
from . import structured_collections as collections

_MAX_WHY_BYTES = 512
_MAX_VALUE_BYTES = 32 * 1024
_MAX_ITEM_FILES = 2_000
_SYSTEM_FIELDS = frozenset({"type", "collection_id", "record_id", "schema_version", "item_version"})
_RECEIPT_MARKER = "exomem.records-mutation"
_RECEIPT_VERSION = 1
_AUDIT_MARKER = re.compile(r"exomem-record-audit:\s*([0-9a-f]{24})")
_MAX_AUDIT_SOURCE_BYTES = 2 * 1024 * 1024
_MAX_AUDIT_MARKERS = 10_000


@dataclass(frozen=True, slots=True)
class _AuditMarker:
    transition_id: str
    canonical_path: str
    item_key: str


def append_record(
    vault_root: Path,
    collection: str | Path | collections.CollectionManifest,
    *,
    item: Mapping[str, Any],
    item_key: str | None = None,
    expected_container_hash: str | None = None,
    why: str,
    body: str = "",
) -> dict[str, Any]:
    """Append one structured item, or return a content-identical replay."""
    root = Path(vault_root)
    _validate_why(why)
    supplied_manifest = _resolve_outside(root, collection)
    if supplied_manifest.storage.strategy == "dataset":
        record_formats.load_adapter(root, supplied_manifest).refuse_mutation("append")
    key = _validate_item_key(item_key or str(uuid.uuid4()))
    values = _validate_values(supplied_manifest, item)
    _validate_body(body)
    if body and supplied_manifest.storage.strategy == "markdown-log":
        raise collections.CollectionError(
            "UNREPRESENTABLE_RECORD_BODY", "markdown-log storage cannot represent item bodies"
        )
    with writer_lease.active_manager().mutation_guard(root, operation="record_append"):
        manifest, manifest_text, manifest_guard = _load_guarded_manifest(root, collection)
        values = _validate_values(manifest, item)
        adapter = record_formats.load_adapter(root, manifest)
        if not adapter.mutable:
            adapter.refuse_mutation("append")
        _require_activity_log(root)
        directory_guards: tuple[vault.DirectoryCensusGuard, ...] = ()
        if manifest.storage.strategy == "markdown-log":
            source_path = root / manifest.storage.source
            source_text, source_guard = vault.read_guarded_text(root, source_path)
            source_bytes = source_text.encode("utf-8")
            snapshot = adapter.read_bytes(  # type: ignore[attr-defined]
                source_bytes, manifest_version=manifest.manifest_version
            )
            current_hash = hashlib.sha256(source_bytes).hexdigest()
        else:
            snapshot = adapter.read()
            directory_guards = _item_directory_guards(root, manifest, snapshot)
            current_hash = snapshot.snapshot
            source_path = root / manifest.storage.source
            source_text = ""
            source_bytes = b""
            source_guard = None
        if expected_container_hash is not None:
            _expect_hash(expected_container_hash, current_hash, "container")
        existing = [record for record in snapshot.records if record.identity.key == key]
        payload_hash = _payload_hash(manifest, key, values, body)
        if existing:
            if len(existing) != 1 or existing[0].ambiguous:
                raise collections.CollectionError("AMBIGUOUS_RECORD", "record key is ambiguous")
            if _payload_hash(manifest, key, existing[0].values, existing[0].body) == payload_hash:
                return _result(
                    operation="append",
                    manifest=manifest,
                    key=key,
                    before_item_hash=existing[0].source.hash,
                    after_item_hash=existing[0].source.hash,
                    before_container_hash=current_hash,
                    after_container_hash=current_hash,
                    affected_paths=[existing[0].source.path],
                    payload_hash=payload_hash,
                    outcome="replayed",
                    audit_correlation=_record_audit_correlation(root, manifest, existing[0]),
                )
            raise collections.CollectionError(
                "RECORD_ID_CONFLICT", "record ID already has different data"
            )
        audit_correlation = _transition_id()
        after_manifest_text = record_formats.render_manifest_audit_head(
            manifest_text, audit_correlation
        )
        after_manifest = collections.parse_manifest_bytes(
            root, root / manifest.path, after_manifest_text.encode("utf-8")
        )
        parent = _manifest_audit_head(manifest_text) or "baseline"
        if manifest.storage.strategy == "markdown-log":
            offset = snapshot.insertion_offset
            if offset is None:
                raise collections.CollectionError(
                    "INVALID_STORAGE_DESCRIPTOR", "log insertion is missing"
                )
            replacement = record_formats.render_markdown_log_item(
                manifest, values, key, _newline(source_bytes), audit_correlation
            )
            after_text = (
                source_bytes[:offset] + replacement.encode("utf-8") + source_bytes[offset:]
            ).decode("utf-8")
            item_hash = hashlib.sha256(replacement.encode("utf-8")).hexdigest()
            canonical_path = source_path
            canonical_guard = source_guard
        else:
            canonical_path, new_item_guards = _new_item_path(root, manifest, key)
            directory_guards = (*directory_guards, *new_item_guards)
            replacement = record_formats.render_markdown_item(
                manifest, values, key, body, audit_correlation
            )
            after_text = replacement
            item_hash = hashlib.sha256(replacement.encode("utf-8")).hexdigest()
            canonical_guard = vault.PathGuard.capture(
                root, canonical_path.relative_to(root).as_posix(), leaf_policy="absent"
            )
        after_hash = (
            hashlib.sha256(after_text.encode("utf-8")).hexdigest()
            if manifest.storage.strategy == "markdown-log"
            else _items_container_hash(
                after_manifest,
                snapshot,
                collections.SourceVersion(canonical_path.relative_to(root).as_posix(), item_hash),
            )
        )
        audit = _audit_body(
            transition_id=audit_correlation,
            parent_id=parent,
            operation="append",
            manifest=manifest,
            item_key=key,
            canonical_path=canonical_path.relative_to(root).as_posix(),
            before_manifest_hash=manifest.manifest_version.hash,
            after_manifest_hash=after_manifest.manifest_version.hash,
            before_item_hash=None,
            after_item_hash=item_hash,
            before_container_hash=current_hash,
            after_container_hash=after_hash,
            payload_hash=payload_hash,
            why=why,
        )
        log_plan = _plan_required_audit(root, manifest, key, audit, after_hash)
        writes = [
            vault.PlannedWrite(
                canonical_path,
                after_text,
                create_only=manifest.storage.strategy == "markdown-items",
                guard=canonical_guard,
            ),
            vault.PlannedWrite(root / manifest.path, after_manifest_text, guard=manifest_guard),
            *log_plan.writes,
        ]
        try:
            vault.batch_atomic_write(
                writes,
                vault_root=root,
                required_guards=(
                    *directory_guards,
                    *_item_snapshot_guards(root, manifest, snapshot),
                ),
            )
        except (vault.PathGuardError, vault.CreateOnlyConflict, OSError, ValueError) as error:
            raise _publication_error(error) from error
        return _result(
            operation="append",
            manifest=manifest,
            key=key,
            before_item_hash=None,
            after_item_hash=item_hash,
            before_container_hash=current_hash,
            after_container_hash=after_hash,
            affected_paths=[canonical_path.relative_to(root).as_posix()],
            payload_hash=payload_hash,
            outcome="committed",
            audit_correlation=audit_correlation,
        )


def update_record(
    vault_root: Path,
    collection: str | Path | collections.CollectionManifest,
    *,
    item_key: str,
    changes: Mapping[str, Any],
    expected_container_hash: str,
    expected_item_version: str,
    why: str,
) -> dict[str, Any]:
    """Apply a guarded, exact-key update to one existing Markdown record."""
    root = Path(vault_root)
    _validate_why(why)
    item_key = _validate_item_key(item_key)
    if not isinstance(changes, Mapping) or not changes:
        raise collections.CollectionError(
            "INVALID_RECORD_CHANGES", "changes must be a non-empty object"
        )
    with writer_lease.active_manager().mutation_guard(root, operation="record_update"):
        manifest, manifest_text, manifest_guard = _load_guarded_manifest(root, collection)
        adapter = record_formats.load_adapter(root, manifest)
        if not adapter.mutable:
            adapter.refuse_mutation("update")
        _require_activity_log(root)
        directory_guards: tuple[vault.DirectoryCensusGuard, ...] = ()
        if manifest.storage.strategy == "markdown-log":
            canonical_source = root / manifest.storage.source
            source_text, source_guard = vault.read_guarded_text(root, canonical_source)
            source_bytes = source_text.encode("utf-8")
            snapshot = adapter.read_bytes(source_bytes, manifest_version=manifest.manifest_version)  # type: ignore[attr-defined]
            _expect_hash(
                expected_container_hash,
                hashlib.sha256(source_bytes).hexdigest(),
                "container",
            )
        else:
            snapshot = adapter.read()
            directory_guards = _item_directory_guards(root, manifest, snapshot)
            source_text = ""
            source_bytes = b""
            source_guard = None
        matches = [record for record in snapshot.records if record.identity.key == item_key]
        if not matches:
            raise collections.CollectionError("RECORD_NOT_FOUND", "record key does not exist")
        if len(matches) != 1 or matches[0].ambiguous:
            raise collections.CollectionError("AMBIGUOUS_RECORD", "record key is ambiguous")
        record = matches[0]
        source_path = root / record.source.path
        if manifest.storage.strategy != "markdown-log":
            source_text, source_guard = vault.read_guarded_text(root, source_path)
            source_bytes = source_text.encode("utf-8")
        current_hash = hashlib.sha256(source_bytes).hexdigest()
        current_item_hash = (
            hashlib.sha256(source_bytes[record.span.start : record.span.end]).hexdigest()
            if manifest.storage.strategy == "markdown-log"
            else current_hash
        )
        if record.source.hash != current_item_hash:
            raise collections.CollectionError(
                "STALE_RECORD", "record changed while resolving update"
            )
        if manifest.storage.strategy != "markdown-log":
            _expect_hash(expected_container_hash, snapshot.snapshot, "container")
        _expect_hash(expected_item_version, record.source.hash, "item")
        merged = dict(record.values)
        merged.update(changes)
        values = _validate_values(manifest, merged)
        audit_correlation = _transition_id()
        after_manifest_text = record_formats.render_manifest_audit_head(
            manifest_text, audit_correlation
        )
        after_manifest = collections.parse_manifest_bytes(
            root, root / manifest.path, after_manifest_text.encode("utf-8")
        )
        parent = _manifest_audit_head(manifest_text) or "baseline"
        if manifest.storage.strategy == "markdown-log":
            replacement = record_formats.render_markdown_log_item(
                manifest, values, item_key, _newline(source_bytes), audit_correlation
            )
            after_text = (
                source_bytes[: record.span.start]
                + replacement.encode("utf-8")
                + source_bytes[record.span.end :]
            ).decode("utf-8")
            canonical_path = source_path
            before_container_hash = current_hash
            after_container_hash = hashlib.sha256(after_text.encode("utf-8")).hexdigest()
        else:
            replacement = record_formats.render_markdown_item_update(
                source_text, changes, audit_correlation
            )
            after_text = replacement
            canonical_path = source_path
            before_container_hash = snapshot.snapshot
            after_container_hash = _items_container_hash(
                after_manifest,
                snapshot,
                collections.SourceVersion(
                    record.source.path, hashlib.sha256(after_text.encode("utf-8")).hexdigest()
                ),
            )
        after_item_hash = hashlib.sha256(replacement.encode("utf-8")).hexdigest()
        audit = _audit_body(
            transition_id=audit_correlation,
            parent_id=parent,
            operation="update",
            manifest=manifest,
            item_key=item_key,
            canonical_path=record.source.path,
            before_manifest_hash=manifest.manifest_version.hash,
            after_manifest_hash=after_manifest.manifest_version.hash,
            before_item_hash=record.source.hash,
            after_item_hash=after_item_hash,
            before_container_hash=before_container_hash,
            after_container_hash=after_container_hash,
            payload_hash=None,
            why=why,
        )
        log_plan = _plan_required_audit(root, manifest, item_key, audit, after_container_hash)
        try:
            vault.batch_atomic_write(
                [
                    vault.PlannedWrite(canonical_path, after_text, guard=source_guard),
                    vault.PlannedWrite(
                        root / manifest.path, after_manifest_text, guard=manifest_guard
                    ),
                    *log_plan.writes,
                ],
                vault_root=root,
                required_guards=(
                    *directory_guards,
                    *_item_snapshot_guards(
                        root, manifest, snapshot, exclude_path=record.source.path
                    ),
                ),
            )
        except (vault.PathGuardError, vault.CreateOnlyConflict, OSError, ValueError) as error:
            raise _publication_error(error) from error
        return _result(
            operation="update",
            manifest=manifest,
            key=item_key,
            before_item_hash=record.source.hash,
            after_item_hash=after_item_hash,
            before_container_hash=before_container_hash,
            after_container_hash=after_container_hash,
            affected_paths=[record.source.path],
            payload_hash=None,
            outcome="committed",
            audit_correlation=audit_correlation,
        )


def create_collection(
    vault_root: Path,
    manifest_path: str | Path,
    manifest_text: str,
    *,
    why: str,
    scaffold: bool = True,
) -> dict[str, Any]:
    """Create a new collection only when its manifest and canonical source are absent."""
    root = Path(vault_root)
    _validate_why(why)
    path = root / manifest_path
    if not isinstance(manifest_text, str) or not manifest_text:
        raise collections.CollectionError(
            "INVALID_COLLECTION_MANIFEST", "manifest text is required"
        )
    with writer_lease.active_manager().mutation_guard(root, operation="record_create"):
        _assert_portable_absent(root, path)
        if path.exists():
            raise collections.CollectionError(
                "CREATE_ONLY_CONFLICT", "collection manifest already exists"
            )
        manifest_guard = vault.PathGuard.capture(
            root, path.relative_to(root).as_posix(), leaf_policy="absent"
        )
        # Parsing the proposed bytes validates its declared storage before staging.
        if path.name != "_collection.md":
            raise collections.CollectionError(
                "INVALID_COLLECTION_MANIFEST", "manifest must be _collection.md"
            )
        collections.parse_manifest_bytes(root, path, manifest_text.encode("utf-8"))
        _require_activity_log(root)
        audit_correlation = _transition_id()
        marked_manifest_text = record_formats.render_manifest_audit_head(
            manifest_text, audit_correlation
        )
        manifest = collections.parse_manifest_bytes(
            root, path, marked_manifest_text.encode("utf-8")
        )
        source = root / manifest.storage.source
        _assert_portable_absent(root, source)
        if source.exists() or _casefold_alias(source):
            raise collections.CollectionError(
                "CREATE_ONLY_CONFLICT", "canonical source already exists"
            )
        writes = [
            vault.PlannedWrite(
                path,
                marked_manifest_text,
                create_only=True,
                guard=manifest_guard,
                ensure_directories=(source,)
                if scaffold and manifest.storage.strategy == "markdown-items"
                else (),
            )
        ]
        affected = [manifest.path]
        if scaffold and manifest.storage.strategy == "markdown-log":
            title = manifest.storage.descriptor["section"]["title"]
            level = manifest.storage.descriptor["section"]["level"]
            source_guard = vault.PathGuard.capture(
                root, manifest.storage.source, leaf_policy="absent"
            )
            writes.append(
                vault.PlannedWrite(
                    source, f"{'#' * level} {title}\n", create_only=True, guard=source_guard
                )
            )
            affected.append(manifest.storage.source)
        elif scaffold and manifest.storage.strategy == "markdown-items":
            affected.append(manifest.storage.source)
        if scaffold and manifest.storage.strategy == "markdown-log":
            after_item_hash = hashlib.sha256(
                f"{'#' * manifest.storage.descriptor['section']['level']} {manifest.storage.descriptor['section']['title']}\n".encode()
            ).hexdigest()
            after_container_hash = after_item_hash
        elif manifest.storage.strategy == "markdown-items":
            after_item_hash = None
            after_container_hash = _empty_items_container_hash(manifest)
        else:
            after_item_hash = None
            after_container_hash = manifest.manifest_version.hash
        audit = _audit_body(
            transition_id=audit_correlation,
            parent_id="absent",
            operation="create",
            manifest=manifest,
            item_key=None,
            canonical_path=manifest.storage.source,
            before_manifest_hash=None,
            after_manifest_hash=manifest.manifest_version.hash,
            before_item_hash=None,
            after_item_hash=after_item_hash,
            before_container_hash=None,
            after_container_hash=after_container_hash,
            payload_hash=None,
            why=why,
        )
        log_plan = _plan_required_audit(root, manifest, "collection", audit, after_container_hash)
        try:
            vault.batch_atomic_write(
                [*writes, *log_plan.writes],
                vault_root=root,
                required_guards=_portable_absence_guards(root, path, source),
            )
        except (vault.PathGuardError, vault.CreateOnlyConflict, OSError, ValueError) as error:
            raise _publication_error(error) from error
        return _result(
            operation="create",
            manifest=manifest,
            key=None,
            before_item_hash=None,
            after_item_hash=after_item_hash,
            before_container_hash=None,
            after_container_hash=after_container_hash,
            affected_paths=affected,
            payload_hash=None,
            outcome="committed",
            audit_correlation=audit_correlation,
        )


def inspect_audit_gap(
    vault_root: Path, collection: str | Path | collections.CollectionManifest
) -> dict[str, Any]:
    """Report, without repair, whether current bytes prove an audit transition chain."""
    root = Path(vault_root)
    manifest = _resolve_outside(root, collection)
    manifest_bytes = _safe_audit_read(
        root, manifest.path, manifest.manifest_version.hash, _MAX_AUDIT_SOURCE_BYTES
    )
    if manifest_bytes is None:
        return {"status": "history_incomplete", "gaps": []}
    try:
        head = _manifest_audit_head(manifest_bytes.decode("utf-8-sig"))
    except UnicodeDecodeError:
        return {"status": "history_incomplete", "gaps": []}
    try:
        snapshot = record_formats.load_adapter(root, manifest).read()
    except collections.CollectionError:
        if head is not None:
            return {"status": "gap", "gaps": ["canonical-source-unavailable"]}
        return {"status": "history_incomplete", "gaps": []}
    current_hash = (
        snapshot.source_versions[-1].hash
        if manifest.storage.strategy == "markdown-log"
        else snapshot.snapshot
    )
    markers = _audit_markers(root, manifest, snapshot)
    if markers is None:
        return {"status": "history_incomplete", "gaps": []}
    events, complete = _audit_events(root)
    relevant = [event for event in events if event.get("collection_id") == manifest.collection_id]
    if head is None and not markers and not relevant:
        return {"status": "baseline" if complete else "history_incomplete", "gaps": []}
    if not complete:
        return {
            "status": "history_incomplete",
            "gaps": sorted(marker.transition_id for marker in markers)[:32],
        }
    by_id: dict[str, dict[str, Any]] = {}
    gaps: list[str] = []
    for event in relevant:
        transition = event["transition_id"]
        old = by_id.get(transition)
        if old is None:
            by_id[transition] = event
        elif old != event:
            gaps.append("conflicting-transition:" + transition)
    children: dict[str, set[str]] = {}
    for event in by_id.values():
        children.setdefault(event["parent_id"], set()).add(event["transition_id"])
    for fork_parent, transition_ids in children.items():
        if len(transition_ids) > 1:
            gaps.append("transition-fork:" + fork_parent)
    reachable: set[str] = set()
    if head is None:
        gaps.append("missing-manifest-head")
    current = by_id.get(head or "")
    if current is None:
        gaps.append("missing-head-event")
    else:
        if not _event_matches_transition(current, manifest):
            gaps.append("head-collection-mismatch")
        if current["after_container_hash"] != current_hash:
            gaps.append("current-container-mismatch")
        if current["after_manifest_hash"] != manifest.manifest_version.hash:
            gaps.append("current-manifest-mismatch")
        cursor = current
        depth = 0
        while True:
            reachable.add(cursor["transition_id"])
            if not _event_matches_transition(cursor, manifest):
                gaps.append("invalid-transition:" + cursor["transition_id"])
                break
            if cursor["parent_id"] in {"baseline", "absent"}:
                break
            depth += 1
            if depth > 2048:
                gaps.append("chain-depth")
                break
            predecessor = by_id.get(cursor["parent_id"])
            if predecessor is None:
                gaps.append("missing-parent:" + cursor["parent_id"])
                break
            if (
                cursor["before_container_hash"] != predecessor["after_container_hash"]
                or cursor["before_manifest_hash"] != predecessor["after_manifest_hash"]
            ):
                gaps.append("transition-discontinuity:" + cursor["transition_id"])
                break
            cursor = predecessor
    for marker in markers:
        marker_event = by_id.get(marker.transition_id)
        if (
            marker_event is None
            or marker.transition_id not in reachable
            or not _event_matches_transition(marker_event, manifest)
            or marker_event["canonical_path"] != marker.canonical_path
            or marker_event["item_key"] != marker.item_key
        ):
            gaps.append("unmatched-marker:" + marker.transition_id)
    return {"status": "gap" if gaps else "ok", "gaps": sorted(set(gaps))[:32]}


def _audit_markers(
    root: Path,
    manifest: collections.CollectionManifest,
    snapshot: record_formats.AdapterSnapshot,
) -> tuple[_AuditMarker, ...] | None:
    if manifest.storage.strategy == "markdown-log":
        source = snapshot.source_versions[-1]
        data = _safe_audit_read(root, source.path, source.hash, _MAX_AUDIT_SOURCE_BYTES)
        if data is None:
            return None
        matches = 0
        markers: list[_AuditMarker] = []
        for record in snapshot.records:
            found = _AUDIT_MARKER.findall(data[record.span.start : record.span.end].decode("utf-8"))
            matches += len(found)
            if matches > _MAX_AUDIT_MARKERS:
                return None
            markers.extend(
                _AuditMarker(match, manifest.storage.source, record.identity.key) for match in found
            )
        return tuple(markers)
    markers = []
    for record in snapshot.records:
        data = _safe_audit_read(
            root, record.source.path, record.source.hash, _MAX_AUDIT_SOURCE_BYTES
        )
        if data is None:
            return None
        try:
            found = _AUDIT_MARKER.findall(data.decode("utf-8-sig"))
        except UnicodeDecodeError:
            return None
        if len(markers) + len(found) > _MAX_AUDIT_MARKERS:
            return None
        markers.extend(
            _AuditMarker(match, record.source.path, record.identity.key) for match in found
        )
    return tuple(markers)


def _event_matches_collection(
    event: Mapping[str, Any], manifest: collections.CollectionManifest
) -> bool:
    return (
        event["collection_id"] == manifest.collection_id
        and event["manifest_path"] == manifest.path
        and event["source_path"] == manifest.storage.source
    )


def _event_matches_transition(
    event: Mapping[str, Any], manifest: collections.CollectionManifest
) -> bool:
    if not _event_matches_collection(event, manifest):
        return False
    source = manifest.storage.source
    operation = event["operation"]
    item_key = event["item_key"]
    if operation == "create":
        return (
            event["parent_id"] == "absent"
            and item_key is None
            and event["canonical_path"] == source
            and event["before_manifest_hash"] is None
            and event["before_item_hash"] is None
            and event["before_container_hash"] is None
        )
    if not isinstance(item_key, str) or _validate_audit_item_key(item_key) is None:
        return False
    canonical = event["canonical_path"]
    if manifest.storage.strategy == "markdown-log":
        return canonical == source
    if manifest.storage.strategy != "markdown-items":
        return False
    source_path = Path(source)
    candidate = Path(canonical)
    try:
        candidate.relative_to(source_path)
    except ValueError:
        return False
    return candidate.suffix == ".md" and not candidate.is_absolute() and ".." not in candidate.parts


def _validate_audit_item_key(value: str) -> str | None:
    try:
        return value if str(uuid.UUID(value)) == value else None
    except ValueError:
        return None


def _safe_audit_read(root: Path, relative: str, expected_hash: str, limit: int) -> bytes | None:
    """Read one canonical audit source once, bounded and without following a swap."""
    candidate = root / relative
    try:
        relative_path = candidate.relative_to(root)
        if any(part in {"", ".", ".."} for part in relative_path.parts):
            return None
        before = candidate.lstat()
        if (
            not stat.S_ISREG(before.st_mode)
            or stat.S_ISLNK(before.st_mode)
            or before.st_size > limit
        ):
            return None
        descriptor = os.open(candidate, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except (OSError, ValueError):
        return None
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (
            before.st_dev,
            before.st_ino,
        ):
            return None
        with os.fdopen(os.dup(descriptor), "rb", closefd=True) as stream:
            data = stream.read(before.st_size + 1)
        after = os.fstat(descriptor)
        if (
            len(data) != before.st_size
            or (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            != (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)
            or hashlib.sha256(data).hexdigest() != expected_hash
        ):
            return None
        return data
    except OSError:
        return None
    finally:
        os.close(descriptor)


def _record_audit_correlation(
    root: Path, manifest: collections.CollectionManifest, record: record_formats.Record
) -> str | None:
    if manifest.storage.strategy == "markdown-log":
        text = (
            (root / manifest.storage.source)
            .read_bytes()[record.span.start : record.span.end]
            .decode("utf-8")
        )
    else:
        text = (root / record.source.path).read_text(encoding="utf-8-sig")
    markers = _AUDIT_MARKER.findall(text)
    return markers[0] if len(markers) == 1 else None


def _audit_events(root: Path) -> tuple[list[dict[str, Any]], bool]:
    """Read bounded ordinary log segments without following untrusted archive entries."""
    log_root = root / vault.kb_prefix()
    live = log_root / "log.md"
    archive = log_root / "_archive" / "logs"
    candidates = [live]
    complete = True
    try:
        if archive.exists():
            info = archive.lstat()
            if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
                return [], False
            entries = sorted(archive.iterdir(), key=lambda path: path.name)
            if len(entries) > 128:
                return [], False
            candidates.extend(
                path for path in entries if path.name.startswith("log-") and path.suffix == ".md"
            )
    except OSError:
        return [], False
    events: list[dict[str, Any]] = []
    total = 0
    for path in candidates:
        try:
            info = path.lstat()
        except FileNotFoundError:
            complete = False
            continue
        except OSError:
            return [], False
        if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode) or info.st_size > 2_000_000:
            return [], False
        total += info.st_size
        if total > 8_000_000:
            return [], False
        try:
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            try:
                opened = os.fstat(descriptor)
                if not stat.S_ISREG(opened.st_mode) or opened.st_ino != info.st_ino:
                    return [], False
                data = os.read(descriptor, info.st_size + 1)
                ended = os.fstat(descriptor)
                if len(data) > info.st_size or (
                    ended.st_dev,
                    ended.st_ino,
                    ended.st_size,
                    ended.st_mtime_ns,
                ) != (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns):
                    return [], False
            finally:
                os.close(descriptor)
            text = data.decode("utf-8")
        except (OSError, UnicodeDecodeError):
            return [], False
        for line in text.splitlines():
            if not line.startswith("Records audit-v1 "):
                continue
            try:
                event = json.loads(line.removeprefix("Records audit-v1 "))
            except json.JSONDecodeError:
                return [], False
            if not _valid_audit_event(event):
                return [], False
            events.append(event)
            if len(events) > 10_000:
                return [], False
    return events, complete


def _valid_audit_event(event: Any) -> bool:
    if not isinstance(event, dict) or set(event) != {
        "version",
        "transition_id",
        "parent_id",
        "operation",
        "collection_id",
        "manifest_path",
        "source_path",
        "canonical_path",
        "item_key",
        "before_manifest_hash",
        "after_manifest_hash",
        "before_item_hash",
        "after_item_hash",
        "before_container_hash",
        "after_container_hash",
        "payload_hash",
        "rationale",
    }:
        return False
    hashes = (
        "before_manifest_hash",
        "after_manifest_hash",
        "before_item_hash",
        "after_item_hash",
        "before_container_hash",
        "after_container_hash",
        "payload_hash",
    )
    return (
        type(event["version"]) is int
        and event["version"] == 1
        and isinstance(event["transition_id"], str)
        and re.fullmatch(r"[0-9a-f]{24}", event["transition_id"]) is not None
        and isinstance(event["parent_id"], str)
        and event["operation"] in {"append", "update", "create"}
        and all(
            isinstance(event[name], str) and event[name]
            for name in (
                "collection_id",
                "manifest_path",
                "source_path",
                "canonical_path",
                "rationale",
            )
        )
        and (event["item_key"] is None or isinstance(event["item_key"], str))
        and all(
            value is None or (isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value))
            for name in hashes
            for value in (event[name],)
        )
    )


def _resolve_outside(
    root: Path, collection: str | Path | collections.CollectionManifest
) -> collections.CollectionManifest:
    return (
        collection
        if isinstance(collection, collections.CollectionManifest)
        else collections.resolve_collection(root, collection)
    )


def _load_guarded_manifest(
    root: Path, collection: str | Path | collections.CollectionManifest
) -> tuple[collections.CollectionManifest, str, vault.PathGuard]:
    resolved = _resolve_outside(root, collection)
    path = root / resolved.path
    text, guard = vault.read_guarded_text(root, path)
    parsed = collections.parse_manifest_bytes(root, path, text.encode("utf-8"))
    if isinstance(collection, collections.CollectionManifest) and (
        collection.path != parsed.path
        or collection.collection_id != parsed.collection_id
        or collection.manifest_version.hash != parsed.manifest_version.hash
    ):
        raise collections.CollectionError(
            "STALE_COLLECTION_MANIFEST", "caller collection manifest changed before mutation"
        )
    return parsed, text, guard


def _validate_values(
    manifest: collections.CollectionManifest, item: Mapping[str, Any]
) -> dict[str, Any]:
    if not isinstance(item, Mapping):
        raise collections.CollectionError("INVALID_ITEM", "item must be an object")
    names = set(item)
    if names & _SYSTEM_FIELDS:
        raise collections.CollectionError(
            "RESERVED_RECORD_FIELD", "item uses a reserved system field"
        )
    representational = _log_note_field(manifest)
    unknown = (
        names - set(manifest.schema.fields) - ({representational} if representational else set())
    )
    if unknown:
        raise collections.CollectionError(
            "SCHEMA_UNKNOWN_FIELD", "item uses fields outside the schema"
        )
    schema_values = {name: value for name, value in item.items() if name in manifest.schema.fields}
    value = manifest.schema.validate(schema_values)
    if representational:
        note = item.get(representational)
        if note is not None and type(note) is not str:
            raise collections.CollectionError("SCHEMA_FIELD_TYPE", "heading note must be a string")
        value[representational] = note
    _validate_representable(value)
    return value


def _log_note_field(manifest: collections.CollectionManifest) -> str | None:
    if manifest.storage.strategy != "markdown-log":
        return None
    heading = manifest.storage.descriptor.get("item_heading")
    note = heading.get("note") if isinstance(heading, Mapping) else None
    field = note.get("field") if isinstance(note, Mapping) else None
    return field if isinstance(field, str) else None


def _validate_representable(value: Any) -> None:
    if isinstance(value, str):
        if len(value.encode("utf-8")) > _MAX_VALUE_BYTES or "\r" in value or "\n" in value:
            raise collections.CollectionError(
                "UNREPRESENTABLE_RECORD_VALUE", "record value cannot render losslessly"
            )
    elif isinstance(value, Mapping):
        for item in value.values():
            _validate_representable(item)
    elif isinstance(value, list):
        for item in value:
            _validate_representable(item)


def _validate_why(why: str) -> None:
    if (
        type(why) is not str
        or not why.strip()
        or len(why.encode("utf-8")) > _MAX_WHY_BYTES
        or "\n" in why
        or "\r" in why
    ):
        raise collections.CollectionError(
            "INVALID_RECORD_RATIONALE", "why must be concise plain text"
        )


def _validate_body(body: str) -> None:
    if type(body) is not str or len(body.encode("utf-8")) > _MAX_VALUE_BYTES:
        raise collections.CollectionError("INVALID_RECORD_BODY", "record body is too large")


def _validate_item_key(key: str) -> str:
    normalized = collections.memory_refs.normalize_id(key)
    if normalized is None:
        raise collections.CollectionError("INVALID_RECORD_ID", "record ID must be a UUID")
    return normalized


def _expect_hash(expected: str | None, actual: str, kind: str) -> None:
    if type(expected) is not str or expected != actual:
        raise collections.CollectionError("STALE_RECORD", f"{kind} version is stale")


def _publication_error(error: Exception) -> collections.CollectionError:
    if isinstance(error, vault.PathGuardError):
        return collections.CollectionError("STALE_RECORD", "canonical record changed before commit")
    if isinstance(error, vault.CreateOnlyConflict):
        return collections.CollectionError(
            "CREATE_ONLY_CONFLICT", "canonical target already exists"
        )
    return collections.CollectionError(
        "RECORD_PUBLICATION_FAILED", "record publication did not commit"
    )


def _newline(data: bytes) -> str:
    return "\r\n" if b"\r\n" in data else "\n"


def _new_item_path(
    root: Path, manifest: collections.CollectionManifest, key: str
) -> tuple[Path, tuple[vault.DirectoryCensusGuard, ...]]:
    source = root / manifest.storage.source
    if not source.is_dir():
        raise collections.CollectionError("SOURCE_NOT_FOUND", "item directory could not be read")
    target = source / f"{key}.md"
    if target.exists() or _casefold_alias(target):
        raise collections.CollectionError("RECORD_ID_CONFLICT", "item path already exists")
    return target, (
        vault.DirectoryCensusGuard.capture(
            root, manifest.storage.source, max_entries=_MAX_ITEM_FILES
        ),
    )


def _item_directory_guards(
    root: Path,
    manifest: collections.CollectionManifest,
    snapshot: record_formats.AdapterSnapshot,
) -> tuple[vault.DirectoryCensusGuard, ...]:
    directories = {manifest.storage.source}
    directories.update(
        path for path, kind, _digest in snapshot.source_inventory if kind == "directory"
    )
    return tuple(
        vault.DirectoryCensusGuard.capture(root, directory, max_entries=_MAX_ITEM_FILES)
        for directory in sorted(directories)
    )


def _casefold_alias(path: Path) -> bool:
    if not path.parent.is_dir():
        return False
    wanted = unicodedata.normalize("NFC", path.name).casefold()
    return any(
        unicodedata.normalize("NFC", child.name).casefold() == wanted
        for child in path.parent.iterdir()
    )


def _assert_portable_absent(root: Path, target: Path) -> None:
    """Refuse an alias at any existing NFC/casefold-insensitive path component."""
    try:
        relative = target.relative_to(root)
    except ValueError as error:
        raise collections.CollectionError(
            "INVALID_COLLECTION_PATH", "target escapes vault"
        ) from error
    if target.is_absolute() and not str(target).startswith(str(root)):
        raise collections.CollectionError("INVALID_COLLECTION_PATH", "target escapes vault")
    if not relative.parts or any(component in {"", ".", ".."} for component in relative.parts):
        raise collections.CollectionError("INVALID_COLLECTION_PATH", "target path is not portable")
    current = root
    for component in relative.parts:
        try:
            info = current.lstat()
        except OSError as error:
            raise collections.CollectionError(
                "INVALID_COLLECTION_PATH", "target parent is unreadable"
            ) from error
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise collections.CollectionError("INVALID_COLLECTION_PATH", "target parent is unsafe")
        wanted = unicodedata.normalize("NFC", component).casefold()
        aliases: list[str] = []
        try:
            with os.scandir(current) as entries:
                for index, child in enumerate(entries, 1):
                    if index > _MAX_ITEM_FILES:
                        raise collections.CollectionError(
                            "INVALID_COLLECTION_PATH", "target parent exceeds the entry limit"
                        )
                    if unicodedata.normalize("NFC", child.name).casefold() == wanted:
                        aliases.append(child.name)
        except OSError as error:
            raise collections.CollectionError(
                "INVALID_COLLECTION_PATH", "target parent is unreadable"
            ) from error
        if aliases and component not in aliases:
            raise collections.CollectionError(
                "CREATE_ONLY_CONFLICT", "target conflicts with a portable path alias"
            )
        current /= component
        if not os.path.lexists(current):
            return


def _portable_absence_guards(root: Path, *targets: Path) -> tuple[vault.DirectoryCensusGuard, ...]:
    directories: set[str] = set()
    for target in targets:
        relative = target.relative_to(root)
        for index in range(1, len(relative.parts)):
            directory = root.joinpath(*relative.parts[:index])
            if directory.is_dir():
                directories.add(directory.relative_to(root).as_posix())
    return tuple(
        vault.DirectoryCensusGuard.capture(root, directory, max_entries=_MAX_ITEM_FILES)
        for directory in sorted(directories)
    )


def _require_activity_log(root: Path) -> None:
    if not (root / vault.kb_prefix() / "log.md").is_file():
        raise collections.CollectionError(
            "RECORD_AUDIT_UNAVAILABLE", "Knowledge Base/log.md is required"
        )


def _plan_required_audit(
    root: Path,
    manifest: collections.CollectionManifest,
    key: str,
    body: str,
    token_hash: str,
) -> vault.LogWritePlan:
    plan = vault.plan_log_writes(
        root,
        date_iso=dt.date.today().isoformat(),
        op="record_memory",
        rel_path_no_ext=manifest.storage.source.removesuffix(".md"),
        body=body,
        operation_token=f"records:{manifest.collection_id}:{key}:{token_hash}",
    )
    if plan.warning is not None:
        raise collections.CollectionError(
            "RECORD_AUDIT_UNAVAILABLE", "Knowledge Base/log.md is required"
        )
    return plan


def _audit_body(
    *,
    transition_id: str,
    parent_id: str,
    operation: str,
    manifest: collections.CollectionManifest,
    item_key: str | None,
    canonical_path: str,
    before_manifest_hash: str | None,
    after_manifest_hash: str | None,
    before_item_hash: str | None,
    after_item_hash: str | None,
    before_container_hash: str | None,
    after_container_hash: str | None,
    payload_hash: str | None,
    why: str,
) -> str:
    """Render one strict, content-free activity event."""
    event = {
        "version": 1,
        "transition_id": transition_id,
        "parent_id": parent_id,
        "operation": operation,
        "collection_id": manifest.collection_id,
        "manifest_path": manifest.path,
        "source_path": manifest.storage.source,
        "canonical_path": canonical_path,
        "item_key": item_key,
        "before_manifest_hash": before_manifest_hash,
        "after_manifest_hash": after_manifest_hash,
        "before_item_hash": before_item_hash,
        "after_item_hash": after_item_hash,
        "before_container_hash": before_container_hash,
        "after_container_hash": after_container_hash,
        "payload_hash": payload_hash,
        "rationale": why,
    }
    return "Records audit-v1 " + json.dumps(
        event, ensure_ascii=False, separators=(",", ":"), allow_nan=False, sort_keys=True
    )


def _payload_hash(
    manifest: collections.CollectionManifest, key: str, values: Mapping[str, Any], body: str
) -> str:
    ordered = [[name, _normalize_json(values[name])] for name in sorted(values)]
    payload = [manifest.schema.version, key, ordered, _normalize_json(body)]
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode(
            "utf-8"
        )
    ).hexdigest()


def _normalize_json(value: Any) -> Any:
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if isinstance(value, Mapping):
        return {str(key): _normalize_json(item) for key, item in sorted(value.items())}
    if isinstance(value, list):
        return [_normalize_json(item) for item in value]
    return value


def _transition_id() -> str:
    return uuid.uuid4().hex[:24]


def _manifest_audit_head(text: str) -> str | None:
    match = re.search(r"(?m)^record_audit:\s*\{version:\s*1,\s*head:\s*([0-9a-f]{24})\}\s*$", text)
    if match is not None:
        return match.group(1)
    match = re.search(
        r"(?m)^record_audit:[ \t]*\r?\n(?:[ \t]+version:\s*1\s*\r?\n)?[ \t]+head:\s*([0-9a-f]{24})\s*$",
        text,
    )
    return match.group(1) if match is not None else None


def _items_container_hash(
    manifest: collections.CollectionManifest,
    snapshot: record_formats.AdapterSnapshot,
    replacement: collections.SourceVersion,
) -> str:
    inventory = [entry for entry in snapshot.source_inventory if entry[0] != replacement.path]
    inventory.append((replacement.path, "file", replacement.hash))
    inventory.sort()
    pairs = [(manifest.manifest_version.path, manifest.manifest_version.hash)] + [
        (f"{kind}:{path}", digest) for path, kind, digest in inventory
    ]
    return hashlib.sha256(
        json.dumps(pairs, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def _empty_items_container_hash(manifest: collections.CollectionManifest) -> str:
    return hashlib.sha256(
        json.dumps(
            [(manifest.manifest_version.path, manifest.manifest_version.hash)],
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()


def _item_snapshot_guards(
    root: Path,
    manifest: collections.CollectionManifest,
    snapshot: record_formats.AdapterSnapshot,
    *,
    exclude_path: str | None = None,
) -> tuple[vault.PathGuard, ...]:
    if manifest.storage.strategy != "markdown-items":
        return ()
    return tuple(
        vault.PathGuard.capture(
            root,
            version.path,
            leaf_policy="content",
            expected_content_hash=version.hash,
        )
        for version in snapshot.source_versions[1:]
        if version.path != exclude_path
    )


def _result(
    *,
    operation: str,
    manifest: collections.CollectionManifest,
    key: str | None,
    before_item_hash: str | None,
    after_item_hash: str | None,
    before_container_hash: str | None,
    after_container_hash: str | None,
    affected_paths: list[str],
    payload_hash: str | None,
    outcome: str,
    audit_correlation: str | None,
) -> dict[str, Any]:
    return {
        "_record_receipt": _RECEIPT_MARKER,
        "receipt_version": _RECEIPT_VERSION,
        "operation": operation,
        "collection_id": manifest.collection_id,
        "item_key": key,
        "before_item_hash": before_item_hash,
        "after_item_hash": after_item_hash,
        "before_container_hash": before_container_hash,
        "after_container_hash": after_container_hash,
        "affected_paths": affected_paths,
        "payload_hash": payload_hash,
        "outcome": outcome,
        "audit_correlation": audit_correlation,
    }
