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
_LOG_RECORD_ID_MARKER = re.compile(rb"<!--\s*exomem-record-id:\s*([0-9a-f-]{36})\s*-->")
_LOG_AUDIT_MARKER = re.compile(rb"<!--\s*exomem-record-audit:\s*([0-9a-f]{24})\s*-->")
_ITEM_AUDIT_MARKER = re.compile(rb"#\s*exomem-record-audit:\s*([0-9a-f]{24})\s*")
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
            source_bytes, source_guard = _read_record_bytes(root, manifest.storage.source)
            snapshot = adapter.read_bytes(  # type: ignore[attr-defined]
                source_bytes, manifest_version=manifest.manifest_version
            )
            current_hash = hashlib.sha256(source_bytes).hexdigest()
        else:
            snapshot = adapter.read()
            directory_guards = snapshot.directory_guards
            current_hash = snapshot.snapshot
            source_path = root / manifest.storage.source
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
                correlation = _replay_audit_correlation(
                    root, manifest, snapshot, existing[0], payload_hash
                )
                if correlation is None:
                    raise collections.CollectionError(
                        "RECORD_ID_CONFLICT", "record ID lacks a correlated append transition"
                    )
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
                    audit_correlation=correlation,
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
        parent = manifest.audit_head or "baseline"
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
            canonical_path, new_item_guards = _new_item_path(root, manifest, key, snapshot)
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
                    *snapshot.path_guards,
                ),
            )
        except vault.BatchWriteError:
            raise
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
            source_bytes, source_guard = _read_record_bytes(root, manifest.storage.source)
            source_text = source_bytes.decode("utf-8")
            snapshot = adapter.read_bytes(source_bytes, manifest_version=manifest.manifest_version)  # type: ignore[attr-defined]
            _expect_hash(
                expected_container_hash,
                hashlib.sha256(source_bytes).hexdigest(),
                "container",
            )
        else:
            snapshot = adapter.read()
            directory_guards = snapshot.directory_guards
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
            source_bytes, source_guard = _read_record_bytes(root, record.source.path)
            source_text = source_bytes.decode("utf-8")
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
        parent = manifest.audit_head or "baseline"
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
                    *(
                        guard
                        for guard in snapshot.path_guards
                        if guard.target != record.source.path
                    ),
                ),
            )
        except vault.BatchWriteError:
            raise
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
        if not scaffold:
            manifest = collections.parse_manifest_bytes(root, path, manifest_text.encode("utf-8"))
            source = root / manifest.storage.source
            _assert_portable_absent(root, source)
            if source.exists() or _casefold_alias(source):
                raise collections.CollectionError(
                    "CREATE_ONLY_CONFLICT", "canonical source already exists"
                )
            source_guard = vault.PathGuard.capture(
                root, manifest.storage.source, leaf_policy="absent"
            )
            try:
                vault.batch_atomic_write(
                    [
                        vault.PlannedWrite(
                            path, manifest_text, create_only=True, guard=manifest_guard
                        )
                    ],
                    vault_root=root,
                    required_guards=(*_portable_absence_guards(root, path, source), source_guard),
                )
            except vault.BatchWriteError:
                raise
            except (vault.PathGuardError, vault.CreateOnlyConflict, OSError, ValueError) as error:
                raise _publication_error(error) from error
            return _result(
                operation="create",
                manifest=manifest,
                key=None,
                before_item_hash=None,
                after_item_hash=None,
                before_container_hash=None,
                after_container_hash=None,
                affected_paths=[manifest.path],
                payload_hash=None,
                outcome="committed",
                audit_correlation=None,
            )
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
        except vault.BatchWriteError:
            raise
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
    try:
        history_guard = vault.DirectoryCensusGuard.capture(
            root, f"{vault.kb_prefix()}_archive/logs", max_entries=128
        )
    except vault.PathGuardError:
        return {"status": "history_incomplete", "gaps": []}
    manifest = _resolve_outside(root, collection)
    manifest_bytes = _safe_audit_read(
        root, manifest.path, manifest.manifest_version.hash, _MAX_AUDIT_SOURCE_BYTES
    )
    if manifest_bytes is None:
        return {"status": "history_incomplete", "gaps": []}
    try:
        head = collections.parse_manifest_bytes(
            root, root / manifest.path, manifest_bytes
        ).audit_head
    except (UnicodeDecodeError, collections.CollectionError):
        return {"status": "history_incomplete", "gaps": []}
    try:
        snapshot = record_formats.load_adapter(root, manifest).read()
    except collections.CollectionError:
        if head is not None:
            return {"status": "gap", "gaps": ["canonical-source-unavailable"]}
        events, complete = _audit_events(root, history_guard=history_guard)
        if complete and not any(
            event.get("collection_id") == manifest.collection_id for event in events
        ):
            return {"status": "baseline", "gaps": []}
        return {"status": "history_incomplete", "gaps": []}
    current_hash = (
        snapshot.source_versions[-1].hash
        if manifest.storage.strategy == "markdown-log"
        else snapshot.snapshot
    )
    try:
        for path_guard in snapshot.path_guards:
            path_guard.recheck(root)
        for directory_guard in snapshot.directory_guards:
            directory_guard.recheck(root)
    except vault.PathGuardError:
        return {"status": "history_incomplete", "gaps": []}
    markers = _audit_markers(root, manifest, snapshot)
    if markers is None:
        return {"status": "history_incomplete", "gaps": []}
    events, complete = _audit_events(root, history_guard=history_guard)
    relevant = [event for event in events if event.get("collection_id") == manifest.collection_id]
    if head is None and not markers and not relevant:
        return {"status": "baseline" if complete else "history_incomplete", "gaps": []}
    if not complete:
        return {
            "status": "history_incomplete",
            "gaps": sorted(marker.transition_id for marker in markers)[:32],
        }
    all_by_id: dict[str, dict[str, Any]] = {}
    gaps: list[str] = []
    for event in events:
        transition = event["transition_id"]
        old = all_by_id.get(transition)
        if old is None:
            all_by_id[transition] = event
        elif old != event:
            gaps.append("conflicting-transition:" + transition)
    by_id = {
        transition: event
        for transition, event in all_by_id.items()
        if event["collection_id"] == manifest.collection_id
    }
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
    for transition in by_id:
        if transition not in reachable:
            gaps.append("unreachable-transition:" + transition)
    for transition, event in all_by_id.items():
        if event["collection_id"] != manifest.collection_id and event["parent_id"] in reachable:
            gaps.append("foreign-child:" + transition)
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
        data = next((data for path, data in snapshot.source_bytes if path == source.path), None)
        if data is None or hashlib.sha256(data).hexdigest() != source.hash:
            return None
        matches = 0
        markers: list[_AuditMarker] = []
        for record in snapshot.records:
            marker = _structural_audit_marker(manifest, data, record)
            if marker is not None:
                matches += 1
                if matches > _MAX_AUDIT_MARKERS:
                    return None
                markers.append(_AuditMarker(marker, manifest.storage.source, record.identity.key))
        return tuple(markers)
    markers = []
    for record in snapshot.records:
        data = next(
            (data for path, data in snapshot.source_bytes if path == record.source.path), None
        )
        if data is None or hashlib.sha256(data).hexdigest() != record.source.hash:
            return None
        marker = _structural_audit_marker(manifest, data, record)
        if marker is not None:
            if len(markers) >= _MAX_AUDIT_MARKERS:
                return None
            markers.append(_AuditMarker(marker, record.source.path, record.identity.key))
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
    if not _valid_audit_event(event) or not _event_matches_collection(event, manifest):
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
    try:
        return vault.read_bounded_guarded_bytes(
            root, relative, limit=limit, expected_hash=expected_hash
        )[0]
    except vault.PathGuardError:
        return None


def _record_audit_correlation(
    manifest: collections.CollectionManifest,
    snapshot: record_formats.AdapterSnapshot,
    record: record_formats.Record,
) -> str | None:
    data = next((data for path, data in snapshot.source_bytes if path == record.source.path), None)
    if data is None:
        return None
    return _structural_audit_marker(manifest, data, record)


def _structural_audit_marker(
    manifest: collections.CollectionManifest, data: bytes, record: record_formats.Record
) -> str | None:
    if manifest.storage.strategy == "markdown-log":
        lines = data[record.span.start : record.span.end].splitlines()
        if len(lines) < 3:
            return None
        identifier = _LOG_RECORD_ID_MARKER.fullmatch(lines[1].strip())
        audit = _LOG_AUDIT_MARKER.fullmatch(lines[2].strip())
        if (
            identifier is None
            or audit is None
            or identifier.group(1).decode("ascii") != record.identity.key
        ):
            return None
        return audit.group(1).decode("ascii")
    item = data.removeprefix(b"\xef\xbb\xbf")
    opening = re.match(rb"\A---\r?\n", item)
    if opening is None:
        return None
    closing = re.search(rb"(?m)^---\r?$", item[opening.end() :])
    if closing is None:
        return None
    frontmatter = item[opening.end() : opening.end() + closing.start()]
    lines = frontmatter.splitlines()
    if not lines:
        return None
    audit = _ITEM_AUDIT_MARKER.fullmatch(lines[-1].strip())
    return audit.group(1).decode("ascii") if audit is not None else None


def _replay_audit_correlation(
    root: Path,
    manifest: collections.CollectionManifest,
    snapshot: record_formats.AdapterSnapshot,
    record: record_formats.Record,
    payload_hash: str,
) -> str | None:
    correlation = _record_audit_correlation(manifest, snapshot, record)
    if correlation is None:
        return None
    events, complete = _audit_events(root)
    if not complete:
        return None
    matching = [
        event
        for event in events
        if event.get("transition_id") == correlation
        and _event_matches_transition(event, manifest)
        and event["operation"] == "append"
        and event["item_key"] == record.identity.key
        and event["canonical_path"] == record.source.path
        and event["after_item_hash"] == record.source.hash
        and event["payload_hash"] == payload_hash
    ]
    return correlation if len(matching) == 1 else None


def _audit_events(
    root: Path,
    *,
    history_guard: vault.DirectoryCensusGuard | None = None,
    history_stamps: tuple[tuple[str, int, int], ...] | None = None,
) -> tuple[list[dict[str, Any]], bool]:
    """Read bounded ordinary log segments without following untrusted archive entries."""
    live = f"{vault.kb_prefix()}log.md"
    archive = f"{vault.kb_prefix()}_archive/logs"
    candidates = [live]
    archive_guard: vault.DirectoryCensusGuard | None = None
    try:
        archive_guard = history_guard or vault.DirectoryCensusGuard.capture(
            root, archive, max_entries=128
        )
        if archive_guard.directory_identity is not None:
            entries = archive_guard.entries
            if len(entries) > 128:
                return [], False
            candidates.extend(
                entry.relative_path
                for entry in entries
                if re.fullmatch(r"log-[0-9a-f]{20}\.md", Path(entry.relative_path).name)
            )
    except vault.PathGuardError:
        return [], False
    events: list[dict[str, Any]] = []
    file_guards: list[vault.PathGuard] = []
    total = 0
    for relative in candidates:
        try:
            data, file_guard = vault.read_bounded_guarded_bytes(root, relative, limit=2_000_000)
            text = data.decode("utf-8")
        except (vault.PathGuardError, UnicodeDecodeError):
            return [], False
        total += len(data)
        file_guards.append(file_guard)
        if total > 8_000_000:
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
    try:
        if archive_guard is not None:
            archive_guard.recheck(root)
        for file_guard in file_guards:
            file_guard.recheck(root)
    except vault.PathGuardError:
        return [], False
    return events, True


def _valid_audit_event(event: Any) -> bool:
    if not _audit_event_syntax(event):
        return False
    if event["operation"] == "create":
        return (
            event["parent_id"] == "absent"
            and event["item_key"] is None
            and event["before_manifest_hash"] is None
            and event["before_item_hash"] is None
            and event["before_container_hash"] is None
            and event["after_manifest_hash"] is not None
            and event["after_container_hash"] is not None
            and event["payload_hash"] is None
        )
    if event["parent_id"] == "absent" or _validate_audit_item_key(event["item_key"] or "") is None:
        return False
    if event["operation"] == "append":
        return (
            event["before_item_hash"] is None
            and event["after_item_hash"] is not None
            and event["before_manifest_hash"] is not None
            and event["after_manifest_hash"] is not None
            and event["before_container_hash"] is not None
            and event["after_container_hash"] is not None
            and event["payload_hash"] is not None
        )
    return (
        event["before_item_hash"] is not None
        and event["after_item_hash"] is not None
        and event["before_manifest_hash"] is not None
        and event["after_manifest_hash"] is not None
        and event["before_container_hash"] is not None
        and event["after_container_hash"] is not None
        and event["payload_hash"] is None
    )


def _audit_event_syntax(event: Any) -> bool:
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
    parent_is_valid = isinstance(event["parent_id"], str) and (
        event["parent_id"] in {"baseline", "absent"}
        or re.fullmatch(r"[0-9a-f]{24}", event["parent_id"]) is not None
    )
    common = (
        type(event["version"]) is int
        and event["version"] == 1
        and isinstance(event["transition_id"], str)
        and re.fullmatch(r"[0-9a-f]{24}", event["transition_id"]) is not None
        and parent_is_valid
        and event["operation"] in {"append", "update", "create"}
        and _validate_audit_item_key(event["collection_id"]) is not None
        and all(
            _safe_audit_path(event[name])
            for name in ("manifest_path", "source_path", "canonical_path")
        )
        and type(event["rationale"]) is str
        and bool(event["rationale"].strip())
        and "\n" not in event["rationale"]
        and "\r" not in event["rationale"]
        and len(event["rationale"].encode("utf-8")) <= _MAX_WHY_BYTES
        and (event["item_key"] is None or isinstance(event["item_key"], str))
        and all(
            value is None or (isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value))
            for name in hashes
            for value in (event[name],)
        )
    )
    return common


def _safe_audit_path(value: Any) -> bool:
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > 1024:
        return False
    parts = value.split("/")
    return (
        not value.startswith("/")
        and re.match(r"^[A-Za-z]:", value) is None
        and "\\" not in value
        and "\0" not in value
        and all(part not in {"", ".", ".."} for part in parts)
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
    data, guard = _read_record_bytes(root, resolved.path)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise collections.CollectionError(
            "INVALID_COLLECTION_MANIFEST", "manifest is not UTF-8"
        ) from error
    parsed = collections.parse_manifest_bytes(root, path, data)
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
    root: Path,
    manifest: collections.CollectionManifest,
    key: str,
    snapshot: record_formats.AdapterSnapshot,
) -> tuple[Path, tuple[vault.DirectoryCensusGuard, ...]]:
    source = root / manifest.storage.source
    source_guard = next(
        (guard for guard in snapshot.directory_guards if guard.target == manifest.storage.source),
        None,
    )
    if source_guard is None or source_guard.directory_identity is None:
        raise collections.CollectionError("SOURCE_NOT_FOUND", "item directory could not be read")
    target = source / f"{key}.md"
    target_name = target.name.casefold()
    if any(
        Path(entry.relative_path).name.casefold() == target_name for entry in source_guard.entries
    ):
        raise collections.CollectionError("RECORD_ID_CONFLICT", "item path already exists")
    return target, (source_guard,)


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


def _read_record_bytes(root: Path, relative: str) -> tuple[bytes, vault.PathGuard]:
    """Read mutable record state through the bounded descriptor-rooted reader."""
    try:
        return vault.read_bounded_guarded_bytes(root, relative, limit=_MAX_AUDIT_SOURCE_BYTES)
    except vault.PathGuardError as error:
        raise collections.CollectionError(
            "SOURCE_NOT_FOUND", "canonical record source could not be read"
        ) from error


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
            [
                (manifest.manifest_version.path, manifest.manifest_version.hash),
                (f"directory:{manifest.storage.source}", ""),
            ],
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
