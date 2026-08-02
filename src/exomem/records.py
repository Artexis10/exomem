"""Guarded, profile-neutral structured-record mutations."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import unicodedata
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from . import record_formats, vault, writer_lease
from . import structured_collections as collections

_MAX_WHY_BYTES = 512
_MAX_VALUE_BYTES = 32 * 1024
_MAX_ITEM_FILES = 2_000
_SYSTEM_FIELDS = frozenset({"type", "collection_id", "record_id", "schema_version", "item_version"})


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
    key = item_key or str(uuid.uuid4())
    _validate_item_key(key)
    values = _validate_values(_resolve_outside(root, collection), item)
    _validate_body(body)
    with writer_lease.active_manager().mutation_guard(root, operation="record_append"):
        manifest, manifest_guard = _load_guarded_manifest(root, collection)
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
            directory_guards = (
                vault.DirectoryCensusGuard.capture(
                    root, manifest.storage.source, max_entries=_MAX_ITEM_FILES
                ),
            )
            snapshot = adapter.read()
            current_hash = snapshot.snapshot
            source_path = root / manifest.storage.source
            source_text = ""
            source_bytes = b""
            source_guard = None
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
                    audit_correlation=None,
                )
            raise collections.CollectionError(
                "RECORD_ID_CONFLICT", "record ID already has different data"
            )
        if manifest.storage.strategy == "markdown-log":
            offset = snapshot.insertion_offset
            if offset is None:
                raise collections.CollectionError(
                    "INVALID_STORAGE_DESCRIPTOR", "log insertion is missing"
                )
            replacement = record_formats.render_markdown_log_item(
                manifest, values, key, _newline(source_bytes)
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
            replacement = record_formats.render_markdown_item(manifest, values, key, body)
            after_text = replacement
            item_hash = hashlib.sha256(replacement.encode("utf-8")).hexdigest()
            canonical_guard = vault.PathGuard.capture(
                root, canonical_path.relative_to(root).as_posix(), leaf_policy="absent"
            )
        after_hash = hashlib.sha256(after_text.encode("utf-8")).hexdigest()
        audit = _audit_body(
            "append",
            manifest.collection_id,
            key,
            None,
            item_hash,
            current_hash,
            after_hash,
            payload_hash,
            why,
        )
        log_plan = _plan_required_audit(root, manifest, key, audit, after_hash)
        writes = [
            vault.PlannedWrite(
                canonical_path,
                after_text,
                create_only=manifest.storage.strategy == "markdown-items",
                guard=canonical_guard,
            ),
            *log_plan.writes,
        ]
        try:
            vault.batch_atomic_write(
                writes,
                vault_root=root,
                required_guards=(manifest_guard, *directory_guards),
            )
        except vault.PathGuardError as error:
            raise collections.CollectionError(
                "STALE_RECORD", "canonical record changed before commit"
            ) from error
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
            audit_correlation=_audit_correlation(manifest.collection_id, key, after_hash),
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
    _validate_item_key(item_key)
    if not isinstance(changes, Mapping) or not changes:
        raise collections.CollectionError(
            "INVALID_RECORD_CHANGES", "changes must be a non-empty object"
        )
    with writer_lease.active_manager().mutation_guard(root, operation="record_update"):
        manifest, manifest_guard = _load_guarded_manifest(root, collection)
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
            directory_guards = (
                vault.DirectoryCensusGuard.capture(
                    root, manifest.storage.source, max_entries=_MAX_ITEM_FILES
                ),
            )
            snapshot = adapter.read()
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
            _expect_hash(expected_container_hash, current_hash, "container")
        _expect_hash(expected_item_version, record.source.hash, "item")
        merged = dict(record.values)
        merged.update(changes)
        values = _validate_values(manifest, merged)
        if manifest.storage.strategy == "markdown-log":
            replacement = record_formats.render_markdown_log_item(
                manifest, values, item_key, _newline(source_bytes)
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
            replacement = record_formats.render_markdown_item_update(source_text, changes)
            after_text = replacement
            canonical_path = source_path
            before_container_hash = current_hash
            after_container_hash = hashlib.sha256(after_text.encode("utf-8")).hexdigest()
        after_item_hash = hashlib.sha256(replacement.encode("utf-8")).hexdigest()
        audit = _audit_body(
            "update",
            manifest.collection_id,
            item_key,
            record.source.hash,
            after_item_hash,
            before_container_hash,
            after_container_hash,
            None,
            why,
        )
        log_plan = _plan_required_audit(root, manifest, item_key, audit, after_container_hash)
        try:
            vault.batch_atomic_write(
                [
                    vault.PlannedWrite(canonical_path, after_text, guard=source_guard),
                    *log_plan.writes,
                ],
                vault_root=root,
                required_guards=(manifest_guard, *directory_guards),
            )
        except vault.PathGuardError as error:
            raise collections.CollectionError(
                "STALE_RECORD", "canonical record changed before commit"
            ) from error
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
            audit_correlation=_audit_correlation(
                manifest.collection_id, item_key, after_container_hash
            ),
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
        manifest = collections.parse_manifest_bytes(root, path, manifest_text.encode("utf-8"))
        _require_activity_log(root)
        source = root / manifest.storage.source
        if source.exists() or _casefold_alias(source):
            raise collections.CollectionError(
                "CREATE_ONLY_CONFLICT", "canonical source already exists"
            )
        writes = [
            vault.PlannedWrite(
                path,
                manifest_text,
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
        audit = _audit_body(
            "create", manifest.collection_id, "collection", None, None, None, None, None, why
        )
        log_plan = _plan_required_audit(
            root, manifest, "collection", audit, manifest.manifest_version.hash
        )
        try:
            vault.batch_atomic_write([*writes, *log_plan.writes], vault_root=root)
        except vault.PathGuardError as error:
            raise collections.CollectionError(
                "STALE_RECORD", "collection changed before commit"
            ) from error
        return _result(
            operation="create",
            manifest=manifest,
            key="collection",
            before_item_hash=None,
            after_item_hash=None,
            before_container_hash=None,
            after_container_hash=None,
            affected_paths=affected,
            payload_hash=None,
            outcome="committed",
            audit_correlation=_audit_correlation(
                manifest.collection_id, "collection", manifest.manifest_version.hash
            ),
        )


def inspect_audit_gap(
    vault_root: Path, collection: str | Path | collections.CollectionManifest
) -> dict[str, Any]:
    """Report whether current canonical bytes are evidenced by Records activity history."""
    root = Path(vault_root)
    manifest = _resolve_outside(root, collection)
    snapshot = record_formats.load_adapter(root, manifest).read()
    current = {version.path: version.hash for version in snapshot.source_versions[1:]}
    log = root / vault.kb_prefix() / "log.md"
    if not log.is_file():
        return {"status": "history_unavailable", "gaps": list(current)}
    text = log.read_text(encoding="utf-8")
    evidence = re.findall(
        rf"collection_id={re.escape(manifest.collection_id)} .*?after_container_hash=([0-9a-f]{{64}})",
        text,
    )
    if not evidence:
        return {"status": "history_unavailable", "gaps": list(current)}
    hashes = set(evidence)
    gaps = [path for path, digest in current.items() if digest not in hashes]
    return {"status": "gap" if gaps else "ok", "gaps": gaps}


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
) -> tuple[collections.CollectionManifest, vault.PathGuard]:
    resolved = _resolve_outside(root, collection)
    path = root / resolved.path
    text, guard = vault.read_guarded_text(root, path)
    return collections.parse_manifest_bytes(root, path, text.encode("utf-8")), guard


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
    if representational and representational in item:
        note = item[representational]
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


def _validate_item_key(key: str) -> None:
    try:
        collections.ItemIdentity("00000000-0000-4000-8000-000000000000", key)
    except ValueError as error:
        raise collections.CollectionError("INVALID_RECORD_ID", "record ID is invalid") from error


def _expect_hash(expected: str | None, actual: str, kind: str) -> None:
    if type(expected) is not str or expected != actual:
        raise collections.CollectionError("STALE_RECORD", f"{kind} version is stale")


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


def _casefold_alias(path: Path) -> bool:
    if not path.parent.is_dir():
        return False
    wanted = unicodedata.normalize("NFC", path.name).casefold()
    return any(
        unicodedata.normalize("NFC", child.name).casefold() == wanted
        for child in path.parent.iterdir()
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
    operation: str,
    collection_id: str,
    key: str,
    before_item_hash: str | None,
    after_item_hash: str | None,
    before_container_hash: str | None,
    after_container_hash: str | None,
    payload_hash: str | None,
    why: str,
) -> str:
    fields = {
        "operation": operation,
        "collection_id": collection_id,
        "item_key": key,
        "before_item_hash": before_item_hash or "-",
        "after_item_hash": after_item_hash or "-",
        "before_container_hash": before_container_hash or "-",
        "after_container_hash": after_container_hash or "-",
        "payload_hash": payload_hash or "-",
        "rationale": json.dumps(why, ensure_ascii=False),
    }
    return "Records " + " ".join(f"{name}={value}" for name, value in fields.items())


def _payload_hash(
    manifest: collections.CollectionManifest, key: str, values: Mapping[str, Any], body: str
) -> str:
    ordered = [
        [name, _normalize_json(values.get(name))]
        for name in manifest.schema.fields
        if name in values
    ]
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


def _audit_correlation(collection_id: str, key: str, container_hash: str) -> str:
    return hashlib.sha256(f"{collection_id}\0{key}\0{container_hash}".encode()).hexdigest()[:24]


def _result(
    *,
    operation: str,
    manifest: collections.CollectionManifest,
    key: str,
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
