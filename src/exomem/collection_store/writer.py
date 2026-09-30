"""Preview-only built-in collection mutations, canonical in one SQLite transaction.

The caller owns and closes the lease-bound WriterConnection. This slice queues
projection hashes but publishes no vault files; P2 owns staging and installation.
Governance precommit is an explicitly dark seam for P1a.9, not authorization.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import sqlite3
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

import yaml

from .. import mutation_terminal, planning, record_formats, records, vault, writer_lease
from ..governance.principal import effective_principal
from .. import structured_collections as collections
from . import chain, connection, tokens, types


def _json(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def _row(cursor: sqlite3.Cursor) -> dict[str, Any] | None:
    result = cursor.fetchone()
    return (
        None
        if result is None
        else dict(zip((col[0] for col in cursor.description), result, strict=True))
    )


def _natural_key(manifest: collections.CollectionManifest, values: Mapping[str, Any]) -> str | None:
    if not manifest.schema.natural_key:
        return None
    try:
        return collections.manifest_natural_key(manifest, values)
    except ValueError:
        return None


def governance_precommit_pending(
    conn: sqlite3.Connection, manifest: collections.CollectionManifest, paths: Sequence[str]
) -> None:
    """P1a.9 seam: replace with authorization inside this same transaction.

    This no-op is reachable only in the explicit dark preview. It must never
    become the production authorization boundary.
    """


class CollectionWriter:
    """Generic store writer parameterized by the persisted built-in declaration."""

    def __init__(self, vault_root: Path, handle: connection.WriterConnection) -> None:
        if os.environ.get("EXOMEM_COLLECTION_STORE_PREVIEW") != "1":
            raise connection.CollectionStoreError(
                "COLLECTION_STORE_PREVIEW_REQUIRED", "collection store writers are dark"
            )
        self.root = Path(vault_root)
        self.handle = handle
        self.connection = handle.connection

    def _collection(self, selector: str | Path | collections.CollectionManifest):
        key = (
            selector.collection_id
            if isinstance(selector, collections.CollectionManifest)
            else str(selector)
        )
        row = _row(
            self.connection.execute(
                "SELECT c.*, m.manifest_text FROM collections c JOIN collection_manifests m "
                "ON m.collection_id = c.collection_id AND m.manifest_version = c.manifest_version "
                "WHERE c.collection_id = ? OR c.manifest_path = ?",
                (key, key),
            )
        )
        if row is None:
            raise collections.CollectionError("COLLECTION_NOT_FOUND", "collection was not found")
        manifest = collections.parse_manifest_bytes(
            self.root, row["manifest_path"], row["manifest_text"].encode()
        )
        declared = types.type_for_manifest(manifest)
        if (declared.name, declared.version) != (row["type_name"], row["type_version"]):
            raise types.CollectionTypeError(
                "COLLECTION_TYPE_VERSION_MISMATCH", "collection type differs from release"
            )
        return row, replace(manifest, audit_head=row["audit_head"]), declared

    def _item(self, cid: str, key: str) -> dict[str, Any] | None:
        return _row(
            self.connection.execute(
                "SELECT * FROM items WHERE collection_id = ? AND item_key = ?", (cid, key)
            )
        )

    def _container(self, row: Mapping[str, Any]) -> str:
        return tokens.container_hash(row["collection_id"], row["generation"], row["audit_head"])

    def _version(self, row: Mapping[str, Any]) -> str:
        return tokens.item_version(
            row["collection_id"], row["item_key"], row["row_version"], row["payload_hash"]
        )

    def _identity(
        self, action: str, selector: Any, args: Mapping[str, Any], request_id: str | None
    ):
        identity = (
            request_id if request_id is not None else writer_lease.active_mutation_request_id()
        )
        selector = (
            selector.collection_id
            if isinstance(selector, collections.CollectionManifest)
            else str(selector)
        )
        # Refused candidates can contain non-JSON floats; the existing held codec
        # preserves them without making request identity lossy.
        digest = hashlib.sha256(
            b"exomem-collection-request:v1\0"
            + _json(
                records._encode_held_value(
                    {"action": action, "collection": selector, "arguments": dict(args)}
                )
            ).encode()
        ).hexdigest()
        if identity is not None:
            if not isinstance(identity, str) or not identity:
                raise collections.CollectionError(
                    "INVALID_REQUEST_ID", "request identity must be non-empty"
                )
            stored = self.connection.execute(
                "SELECT request_hash, receipt_json FROM txns WHERE request_id = ?", (identity,)
            ).fetchone()
            if stored is not None:
                if stored[0] != digest:
                    raise collections.CollectionError(
                        "IDEMPOTENCY_KEY_REUSED",
                        "request identity already names different arguments",
                    )
                return identity, digest, json.loads(stored[1])
        return identity, digest, None

    def _txn(
        self,
        row: Mapping[str, Any] | None,
        manifest: collections.CollectionManifest,
        operation: str,
        why: str,
        request_id: str | None,
        request_hash: str,
        *,
        effects: Any = None,
    ):
        seq, previous = chain.recorded_head(self.connection)
        txn_id = self.connection.execute(
            "SELECT COALESCE(MAX(txn_id), 0) + 1 FROM txns"
        ).fetchone()[0]
        generation = 0 if row is None else row["generation"]
        before_manifest = None if row is None else row["manifest_version"]
        after_manifest = 1 if row is None else before_manifest + (operation == "revise")
        transition = records._transition_id()
        committed_at = dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        event = {
            "transition_id": transition,
            "collection_id": manifest.collection_id,
            "operation": operation,
            "generation_before": generation,
            "generation_after": generation + 1,
            "manifest_version_before": before_manifest,
            "manifest_version_after": after_manifest,
            "manifest_hash": manifest.manifest_version.hash,
            "prev_event_hash": None if row is None else row["audit_head"],
            "committed_at": committed_at,
            "why": why,
            "request_hash": request_hash,
            "effects": effects,
        }
        event_hash = hashlib.sha256(
            b"exomem-collection-event:v1\0" + _json(event).encode()
        ).hexdigest()
        return {
            "txn_id": txn_id,
            "transition_id": transition,
            "collection_id": manifest.collection_id,
            "operation": operation,
            "profile_operation": f"plan_{'add' if operation == 'append' else operation}"
            if manifest.semantic_profile == "planning"
            else None,
            "generation_before": generation,
            "generation_after": generation + 1,
            "manifest_version_before": before_manifest,
            "manifest_version_after": after_manifest,
            "actor": effective_principal().audience_id,
            "why": why,
            "request_id": request_id,
            "request_hash": request_hash,
            "committed_at": committed_at,
            "prev_event_hash": event["prev_event_hash"],
            "event_hash": event_hash,
            "commit_seq": seq + 1,
            "store_head_hash": tokens.store_head_hash(previous, seq + 1, event_hash),
            "legacy_event_json": None,
        }

    def _insert_txn(self, txn: dict[str, Any], receipt: dict[str, Any]) -> None:
        if not mutation_terminal.valid_collection_receipt(receipt):
            raise RuntimeError("writer constructed an invalid collection receipt")
        data = {**txn, "receipt_json": _json(receipt)}
        names = list(data)
        self.connection.execute(
            f"INSERT INTO txns ({', '.join(names)}) VALUES ({', '.join('?' for _ in names)})",
            tuple(data.values()),
        )

    def _advance(self, txn: Mapping[str, Any]) -> str:
        self.connection.execute(
            "UPDATE collections SET generation = ?, audit_head = ?, updated_txn = ? WHERE collection_id = ?",
            (txn["generation_after"], txn["event_hash"], txn["txn_id"], txn["collection_id"]),
        )
        return tokens.container_hash(
            txn["collection_id"], txn["generation_after"], txn["event_hash"]
        )

    def _pending(
        self, path: str, cid: str, kind: str, version: int, text: str, row_id: int | None = None
    ) -> None:
        self.connection.execute(
            "INSERT INTO projection_state (path, collection_id, row_id, kind, pending_row_version, pending_sha256, state) "
            "VALUES (?, ?, ?, ?, ?, ?, 'pending') ON CONFLICT(path) DO UPDATE SET "
            "pending_row_version = excluded.pending_row_version, pending_sha256 = excluded.pending_sha256, state = 'pending'",
            (path, cid, row_id, kind, version, hashlib.sha256(text.encode()).hexdigest()),
        )

    def _manifest(
        self, manifest: collections.CollectionManifest, text: str, version: int, txn_id: int
    ):
        data = yaml.safe_load(text.split("---", 2)[1])
        self.connection.execute(
            "INSERT INTO collection_manifests VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                manifest.collection_id,
                version,
                text,
                manifest.manifest_version.hash,
                _json(data["item_schema"]),
                _json(list(manifest.schema.natural_key)),
                txn_id,
            ),
        )
        self._pending(manifest.path, manifest.collection_id, "manifest", version, text)

    def create_collection(
        self,
        manifest_path: str | Path,
        manifest_text: str,
        *,
        why: str,
        scaffold: bool = True,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        records._validate_why(why)
        with self.handle.transaction():
            identity, digest, replay = self._identity(
                "create",
                manifest_path,
                {"manifest_text": manifest_text, "why": why, "scaffold": scaffold},
                request_id,
            )
            if replay is not None:
                return replay
            manifest = collections.parse_manifest_bytes(
                self.root, manifest_path, manifest_text.encode()
            )
            declared = types.type_for_manifest(manifest)
            if declared.kind == "intended":
                planning.require_planning_profile(manifest)
            records._refuse_excluded_manifest_fields(manifest)
            record_formats.validate_storage_contract(manifest)
            if manifest.storage.strategy == "dataset":
                raise collections.CollectionError(
                    "READ_ONLY_COLLECTION", "datasets remain file-canonical"
                )
            if manifest.view_diagnostics:
                diagnostic = manifest.view_diagnostics[0]
                raise collections.CollectionError(diagnostic.code, diagnostic.reason)
            if self.connection.execute(
                "SELECT 1 FROM collections WHERE collection_id = ? OR manifest_path = ? OR source_path = ?",
                (manifest.collection_id, manifest.path, manifest.storage.source),
            ).fetchone():
                raise collections.CollectionError(
                    "CREATE_ONLY_CONFLICT", "collection manifest already exists"
                )
            # File-mode data cannot be silently adopted by a dark writer.
            records._assert_portable_absent(self.root, self.root / manifest.path)
            records._assert_portable_absent(self.root, self.root / manifest.storage.source)
            txn = self._txn(None, manifest, "create", why, identity, digest)
            types.register_builtins(self.connection, txn_id=txn["txn_id"])
            self.connection.execute(
                "INSERT INTO collections (collection_id, type_name, type_version, manifest_path, source_path, layout, "
                "manifest_version, generation, audit_head, audit_reader_version, created_txn, updated_txn) "
                "VALUES (?, ?, ?, ?, ?, ?, 1, 1, ?, 1, ?, ?)",
                (
                    manifest.collection_id,
                    declared.name,
                    declared.version,
                    manifest.path,
                    manifest.storage.source,
                    manifest.storage.strategy,
                    txn["event_hash"],
                    txn["txn_id"],
                    txn["txn_id"],
                ),
            )
            self._manifest(manifest, manifest_text, 1, txn["txn_id"])
            affected = [manifest.path] + ([manifest.storage.source] if scaffold else [])
            log_text = None
            if scaffold and manifest.storage.strategy == "markdown-log":
                section = manifest.storage.descriptor["section"]
                log_text = f"{'#' * section['level']} {section['title']}\n"
                self.connection.execute(
                    "UPDATE collections SET log_frame_json = ? WHERE collection_id = ?",
                    (_json({"text": log_text}), manifest.collection_id),
                )
                self._pending(manifest.storage.source, manifest.collection_id, "log", 1, log_text)
            receipt = records._result(
                operation="create",
                manifest=manifest,
                key=None,
                before_item_hash=None,
                after_item_hash=hashlib.sha256(log_text.encode()).hexdigest() if log_text else None,
                before_container_hash=None,
                after_container_hash=tokens.container_hash(
                    manifest.collection_id, 1, txn["event_hash"]
                ),
                affected_paths=affected,
                payload_hash=None,
                outcome="committed",
                audit_correlation=txn["transition_id"],
            )
            governance_precommit_pending(self.connection, manifest, affected)
            self._insert_txn(txn, receipt)
        return receipt

    def _validate(self, manifest, declared, key, values, *, before=None, operation="append"):
        if declared.kind == "intended":
            planning.require_planning_profile(manifest)
            values = planning.normalize_item(
                values,
                apply_defaults=operation == "append",
                validate_motivation=planning.motivation_is_governed(manifest),
            )
            planning._validate_declared_text(manifest, values)
            if before is not None:
                planning._require_same_area_side(before, values)
        values = records._validate_values(manifest, values)
        if declared.validators:
            plans = {
                k: json.loads(v)
                for k, v in self.connection.execute(
                    "SELECT item_key, values_json FROM items WHERE collection_id = ?",
                    (manifest.collection_id,),
                )
            }
            plans[key] = values
            for name in declared.validators:
                types.named_validator(name).validate(manifest, plans)
        return values

    def _held(self, cid: str, held: str | None):
        reference = records._validate_held_reference(held)
        if reference is None:
            return None
        row = _row(
            self.connection.execute(
                "SELECT * FROM held_candidates WHERE held_id = ? AND collection_id = ? AND kind = 'write-refusal'",
                (reference, cid),
            )
        )
        if row is None:
            raise records._held_not_found()
        row["candidate"] = records._decode_held_value(json.loads(row["candidate_json"]))
        return row

    def _hold(self, manifest, error, candidate, why, hold, resumed=None, key=None):
        if (
            not records._holding_enabled(manifest, hold)
            or error.code not in records._CANDIDATE_CONTENT_CODES
        ):
            raise error
        if not records._held_directory_is_outside_source(manifest):
            return collections.CollectionError(
                error.code,
                error.reason,
                {**error.details, "warnings": [records._HOLD_FAILED_WARNING]},
            )
        reference = (
            resumed["held_id"]
            if resumed
            else records._derive_held_id(manifest, candidate.get("item"))
        )
        path = f"{records._held_directory(manifest)}/{reference}.md"
        encoded = _json(records._encode_held_value(candidate))
        if len(encoded.encode()) > records._MAX_HELD_BYTES:
            return collections.CollectionError(
                error.code,
                error.reason,
                {**error.details, "warnings": [records._HOLD_FAILED_WARNING]},
            )
        diagnostics = error.details.get("issues", [])[: records._MAX_HELD_DIAGNOSTICS]
        now = dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        candidate_body = json.dumps(
            records._encode_held_value(candidate),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
            default=records._held_json_default,
        )
        frontmatter = {
            "type": "held-record",
            "collection_id": manifest.collection_id,
            "held_id": reference,
            "attempted_action": candidate["action"],
            "held_at": now,
            "why": why,
            "candidate_sha256": hashlib.sha256(candidate_body.encode()).hexdigest(),
            "diagnostics": _json(diagnostics),
            **({"target_item_key": key} if key else {}),
        }
        text = (
            "---\n"
            + vault.serialize_frontmatter(frontmatter)
            + "\n---\n\n```json\n"
            + candidate_body
            + "\n```\n"
        )
        self.connection.execute(
            "INSERT INTO held_candidates (held_id, collection_id, kind, code, candidate_json, diagnostics_json, view_path, updated_at, held_bytes) "
            "VALUES (?, ?, 'write-refusal', ?, ?, ?, ?, ?, ?) ON CONFLICT(held_id) DO UPDATE SET "
            "code = excluded.code, candidate_json = excluded.candidate_json, diagnostics_json = excluded.diagnostics_json, updated_at = excluded.updated_at, held_bytes = excluded.held_bytes",
            (
                reference,
                manifest.collection_id,
                error.code,
                encoded,
                _json(diagnostics),
                path,
                now,
                text.encode(),
            ),
        )
        self._pending(path, manifest.collection_id, "held", 1, text)
        governance_precommit_pending(self.connection, manifest, [path])
        return collections.CollectionError(
            error.code,
            error.reason,
            {
                **error.details,
                "held": {"held_id": reference, "path": path, "diagnostics": diagnostics},
            },
        )

    def _release(self, held):
        if held:
            self.connection.execute(
                "DELETE FROM projection_state WHERE path = ? AND kind = 'held'",
                (held["view_path"],),
            )
            self.connection.execute(
                "DELETE FROM held_candidates WHERE held_id = ?", (held["held_id"],)
            )

    def _twins(self, cid, key, natural_key):
        if natural_key is not None:
            twins = [
                row[0]
                for row in self.connection.execute(
                    "SELECT item_key FROM items WHERE collection_id = ? AND natural_key = ? AND item_key != ? ORDER BY item_key",
                    (cid, natural_key, key),
                )
            ]
            if twins:
                raise collections.CollectionError(
                    "RECORD_NATURAL_KEY_CONFLICT",
                    "an existing item already holds this natural key under another identity",
                    {"item_keys": twins},
                )

    def _sources(self, sources: Sequence[str]):
        validated, guards = [], []
        cache = {}
        for source in sources:
            resolved = records._resolve_bulk_source(self.root, source, lambda path: True, cache)
            if resolved is None:
                raise collections.CollectionError(
                    "INVALID_RECORD_SOURCE", "source must be a preserved Sources or Evidence page"
                )
            relative, guard = resolved
            validated.append(relative)
            guards.append(guard)
        return validated, guards

    def _write_item(self, manifest, key, values, body, path, txn, before, resumed, sources):
        natural_key = _natural_key(manifest, values)
        payload = tokens.payload_hash(manifest.schema.version, key, values, body)
        version = 1 if before is None else before["row_version"] + 1
        if before is None:
            cursor = self.connection.execute(
                "INSERT INTO items (collection_id, item_key, natural_key, row_version, schema_version, values_json, body, payload_hash, view_path, created_txn, updated_txn) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    manifest.collection_id,
                    key,
                    natural_key,
                    version,
                    manifest.schema.version,
                    _json(values),
                    body,
                    payload,
                    path,
                    txn["txn_id"],
                    txn["txn_id"],
                ),
            )
            row_id = cursor.lastrowid
        else:
            row_id = before["row_id"]
            self.connection.execute(
                "UPDATE items SET natural_key = ?, row_version = ?, values_json = ?, body = ?, payload_hash = ?, updated_txn = ? WHERE row_id = ?",
                (natural_key, version, _json(values), body, payload, txn["txn_id"], row_id),
            )
        self.connection.execute(
            "INSERT INTO item_versions VALUES (?, ?, ?, ?, ?, ?)",
            (row_id, version, _json(values), body, payload, txn["txn_id"]),
        )
        for ordinal, source in enumerate(sources):
            self.connection.execute(
                "INSERT INTO item_sources VALUES (?, ?, ?, ?)", (row_id, version, ordinal, source)
            )
        self.connection.execute(
            "INSERT INTO audit_effects (txn_id, ordinal, row_id, item_key, effect, effect_label, version_before, version_after, hash_before, hash_after) VALUES (?, 0, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                txn["txn_id"],
                row_id,
                key,
                "resume" if resumed else "insert" if before is None else "update",
                "replan"
                if manifest.semantic_profile == "planning"
                else "correction"
                if before
                else None,
                None if before is None else before["row_version"],
                version,
                None if before is None else before["payload_hash"],
                payload,
            ),
        )
        if manifest.storage.strategy == "markdown-items":
            text = record_formats.render_markdown_item(manifest, values, key, body)
            self._pending(path, manifest.collection_id, "item", version, text, row_id)
        else:
            frame_json = self.connection.execute(
                "SELECT log_frame_json FROM collections WHERE collection_id = ?",
                (manifest.collection_id,),
            ).fetchone()[0]
            section = manifest.storage.descriptor["section"]
            frame = (
                json.loads(frame_json)["text"]
                if frame_json
                else f"{'#' * section['level']} {section['title']}\n"
            )
            order = (
                "DESC" if manifest.storage.descriptor.get("insertion") == "newest-first" else "ASC"
            )
            blocks = self.connection.execute(
                f"SELECT item_key, values_json FROM items WHERE collection_id = ? ORDER BY created_txn {order}, row_id {order}",
                (manifest.collection_id,),
            )
            text = frame + "".join(
                record_formats.render_markdown_log_item(manifest, json.loads(v), k, "\n")
                for k, v in blocks
            )
            self._pending(
                manifest.storage.source,
                manifest.collection_id,
                "log",
                txn["generation_after"],
                text,
            )
        self._release(resumed)
        return tokens.item_version(manifest.collection_id, key, version, payload)

    def append_record(
        self,
        collection,
        *,
        item=None,
        item_key=None,
        expected_container_hash=None,
        why,
        body=None,
        delivery=None,
        hold=True,
        held=None,
        sources=(),
        request_id=None,
    ):
        records._validate_why(why)
        hold = records._validate_hold(hold)
        arguments = dict(
            item=item,
            item_key=item_key,
            expected_container_hash=expected_container_hash,
            why=why,
            body=body,
            delivery=delivery,
            hold=hold,
            held=held,
            sources=list(sources),
        )
        with self.handle.transaction():
            identity, digest, replay = self._identity("append", collection, arguments, request_id)
            if replay is not None:
                return replay
            row, manifest, declared = self._collection(collection)
            resumed = self._held(manifest.collection_id, held)
            if item is None and resumed is None:
                raise collections.CollectionError("INVALID_ITEM", "item must be an object")
            values = (
                records._apply_held_overrides(resumed["candidate"].get("item", {}), item or {})
                if resumed
                else item
            )
            if resumed and resumed["candidate"]["action"] != "append":
                raise records._held_not_found()
            body = (
                (resumed["candidate"].get("body") or "") if body is None and resumed else body or ""
            )
            records._validate_body(body)
            records._refuse_excluded_authored_names(values)
            if manifest.storage.strategy == "markdown-log" and body:
                raise collections.CollectionError(
                    "UNREPRESENTABLE_RECORD_BODY",
                    "markdown-log storage cannot represent item bodies",
                )
            key = (
                records._validate_item_key(item_key, manifest=manifest, candidate=values)
                if item_key
                else collections.derived_item_key(manifest, values) or str(uuid.uuid4())
            )
            try:
                values = self._validate(manifest, declared, key, values)
            except collections.CollectionError as error:
                result = self._hold(
                    manifest,
                    error,
                    {
                        "action": "append",
                        "item": dict(values),
                        "item_key": item_key,
                        "body": body,
                        "guards": {"expected_container_hash": expected_container_hash},
                    },
                    why,
                    hold,
                    resumed,
                )
            else:
                before_container = self._container(row)
                if expected_container_hash is not None:
                    records._expect_hash(expected_container_hash, before_container, "container")
                self._twins(manifest.collection_id, key, _natural_key(manifest, values))
                existing = self._item(manifest.collection_id, key)
                payload = tokens.payload_hash(manifest.schema.version, key, values, body)
                delivery_guard = (
                    records._validate_artifact_delivery(self.root, manifest, values, delivery)
                    if delivery is not None
                    else None
                )
                if existing is not None:
                    insert = self.connection.execute(
                        "SELECT t.transition_id FROM audit_effects e JOIN txns t ON t.txn_id = e.txn_id WHERE e.row_id = ? AND e.version_before IS NULL AND e.effect IN ('insert', 'resume') ORDER BY e.txn_id LIMIT 1",
                        (existing["row_id"],),
                    ).fetchone()
                    if existing["payload_hash"] != payload or insert is None:
                        raise collections.CollectionError(
                            "RECORD_ID_CONFLICT", "record ID already has different data"
                        )
                    result = records._result(
                        operation="append",
                        manifest=manifest,
                        key=key,
                        before_item_hash=self._version(existing),
                        after_item_hash=self._version(existing),
                        before_container_hash=before_container,
                        after_container_hash=before_container,
                        affected_paths=[existing["view_path"].split("#")[0]],
                        payload_hash=payload,
                        outcome="replayed",
                        audit_correlation=insert[0],
                    )
                    governance_precommit_pending(
                        self.connection, manifest, result["affected_paths"]
                    )
                    if delivery_guard:
                        delivery_guard.recheck(self.root)
                    self._release(resumed)
                else:
                    if manifest.storage.strategy == "markdown-log":
                        path = f"{manifest.storage.source}#{key}"
                    elif manifest.item_filename:
                        occupied = [
                            r[0]
                            for r in self.connection.execute(
                                "SELECT view_path FROM items WHERE collection_id = ?",
                                (manifest.collection_id,),
                            )
                        ]
                        path = collections.render_item_path(
                            manifest, values, key, occupied_paths=occupied
                        )
                    else:
                        path = f"{manifest.storage.source}/{key}.md"
                    verified, source_guards = self._sources(sources)
                    txn = self._txn(
                        row,
                        manifest,
                        "append",
                        why,
                        identity,
                        digest,
                        effects={"key": key, "payload_hash": payload, "sources": verified},
                    )
                    item_hash = self._write_item(
                        manifest, key, values, body, path, txn, None, resumed, verified
                    )
                    after_container = self._advance(txn)
                    result = records._result(
                        operation="append",
                        manifest=manifest,
                        key=key,
                        before_item_hash=None,
                        after_item_hash=item_hash,
                        before_container_hash=before_container,
                        after_container_hash=after_container,
                        affected_paths=[path.split("#")[0]],
                        payload_hash=payload,
                        outcome="committed",
                        audit_correlation=txn["transition_id"],
                    )
                    governance_precommit_pending(
                        self.connection, manifest, result["affected_paths"]
                    )
                    if delivery_guard:
                        delivery_guard.recheck(self.root)
                    for guard in source_guards:
                        guard.recheck(self.root)
                    self._insert_txn(txn, result)
        if isinstance(result, collections.CollectionError):
            raise result
        return result

    def update_record(
        self,
        collection,
        *,
        item_key,
        changes=None,
        expected_container_hash,
        expected_item_version,
        why,
        operation="update",
        delete_fields=(),
        body=None,
        refresh_presentation=False,
        hold=True,
        held=None,
        sources=(),
        request_id=None,
    ):
        records._validate_why(why)
        hold = records._validate_hold(hold)
        arguments = dict(
            item_key=item_key,
            changes=changes,
            expected_container_hash=expected_container_hash,
            expected_item_version=expected_item_version,
            why=why,
            operation=operation,
            delete_fields=list(delete_fields),
            body=body,
            refresh_presentation=refresh_presentation,
            hold=hold,
            held=held,
            sources=list(sources),
        )
        with self.handle.transaction():
            identity, digest, replay = self._identity(operation, collection, arguments, request_id)
            if replay is not None:
                return replay
            row, manifest, declared = self._collection(collection)
            key = records._validate_item_key(item_key, manifest=manifest, candidate=changes)
            before = self._item(manifest.collection_id, key)
            if before is None:
                raise collections.CollectionError("RECORD_NOT_FOUND", "record key does not exist")
            resumed = self._held(manifest.collection_id, held)
            if resumed:
                candidate = resumed["candidate"]
                if candidate["action"] != "update" or candidate["item_key"] != key:
                    raise records._held_not_found()
                changes = records._apply_held_overrides(candidate.get("changes", {}), changes or {})
                delete_fields = delete_fields or tuple(candidate.get("delete_fields", ()))
                body = candidate.get("body") if body is None else body
            if not isinstance(changes, Mapping) or (
                not changes and not delete_fields and body is None and not refresh_presentation
            ):
                raise collections.CollectionError(
                    "INVALID_RECORD_CHANGES", "changes must be a non-empty object"
                )
            records._refuse_excluded_authored_names(changes)
            old_values = json.loads(before["values_json"])
            final = dict(old_values)
            if declared.kind == "intended":
                if operation == "triage":
                    allowed = {
                        "kind",
                        "status",
                        "priority",
                        "commitment",
                        "horizon",
                        "area",
                        "parent",
                    }
                    if (
                        not changes
                        or set(changes) - allowed
                        or any(
                            v is None and k not in {"area", "parent"} for k, v in changes.items()
                        )
                    ):
                        raise collections.CollectionError(
                            "INVALID_PLAN_ARGUMENTS", "triage transition is invalid"
                        )
                    if old_values["lifecycle"] != "active" or old_values["kind"] == "area":
                        raise collections.CollectionError(
                            "INVALID_PLAN", "triage requires an active deliverable plan"
                        )
                changes, deleted = planning._normalize_changes(manifest, changes)
                delete_fields = tuple(set(delete_fields) | set(deleted))
            for name in delete_fields:
                final.pop(name, None)
            final.update(changes)
            final_body = before["body"] if body is None else body
            records._validate_body(final_body)
            if declared.kind == "intended" and final == old_values and final_body == before["body"]:
                raise collections.CollectionError(
                    "INVALID_PLAN", "Planning update does not change authored state"
                )
            records._expect_hash(expected_container_hash, self._container(row), "container")
            records._expect_hash(expected_item_version, self._version(before), "item")
            try:
                final = self._validate(
                    manifest, declared, key, final, before=old_values, operation=operation
                )
            except collections.CollectionError as error:
                result = self._hold(
                    manifest,
                    error,
                    {
                        "action": "update",
                        "item_key": key,
                        "changes": dict(changes),
                        "delete_fields": list(delete_fields),
                        "body": body,
                        "guards": {
                            "expected_container_hash": expected_container_hash,
                            "expected_item_version": expected_item_version,
                        },
                    },
                    why,
                    hold,
                    resumed,
                    key,
                )
            else:
                self._twins(manifest.collection_id, key, _natural_key(manifest, final))
                verified, source_guards = self._sources(sources)
                txn = self._txn(
                    row,
                    manifest,
                    operation,
                    why,
                    identity,
                    digest,
                    effects={
                        "key": key,
                        "payload_hash": tokens.payload_hash(
                            manifest.schema.version, key, final, final_body
                        ),
                        "sources": verified,
                    },
                )
                item_hash = self._write_item(
                    manifest,
                    key,
                    final,
                    final_body,
                    before["view_path"],
                    txn,
                    before,
                    resumed,
                    verified,
                )
                after_container = self._advance(txn)
                result = records._result(
                    operation=operation,
                    manifest=manifest,
                    key=key,
                    before_item_hash=self._version(before),
                    after_item_hash=item_hash,
                    before_container_hash=self._container(row),
                    after_container_hash=after_container,
                    affected_paths=[before["view_path"].split("#")[0]],
                    payload_hash=None,
                    outcome="committed",
                    audit_correlation=txn["transition_id"],
                )
                governance_precommit_pending(self.connection, manifest, result["affected_paths"])
                for guard in source_guards:
                    guard.recheck(self.root)
                self._insert_txn(txn, result)
        if isinstance(result, collections.CollectionError):
            raise result
        return result

    def revise_collection(
        self,
        collection,
        *,
        manifest_text,
        expected_manifest_hash,
        expected_container_hash,
        why,
        request_id=None,
    ):
        records._validate_why(why)
        with self.handle.transaction():
            identity, digest, replay = self._identity(
                "revise",
                collection,
                dict(
                    manifest_text=manifest_text,
                    expected_manifest_hash=expected_manifest_hash,
                    expected_container_hash=expected_container_hash,
                    why=why,
                ),
                request_id,
            )
            if replay is not None:
                return replay
            row, current, declared = self._collection(collection)
            records._expect_hash(expected_manifest_hash, current.manifest_version.hash, "manifest")
            records._expect_hash(expected_container_hash, self._container(row), "container")
            proposed = collections.parse_manifest_bytes(
                self.root, current.path, manifest_text.encode()
            )
            if (
                proposed.collection_id,
                proposed.semantic_profile,
                proposed.storage,
                proposed.schema.version,
                proposed.schema.natural_key,
            ) != (
                current.collection_id,
                current.semantic_profile,
                current.storage,
                current.schema.version,
                current.schema.natural_key,
            ):
                raise collections.CollectionError(
                    "IMMUTABLE_COLLECTION_REPRESENTATION",
                    "revision cannot migrate collection representation",
                )
            records._refuse_excluded_manifest_fields(proposed)
            record_formats.validate_storage_contract(proposed)
            if declared.kind == "intended":
                planning.require_planning_profile(proposed)
            for key, values in self.connection.execute(
                "SELECT item_key, values_json FROM items WHERE collection_id = ?",
                (current.collection_id,),
            ).fetchall():
                self._validate(proposed, declared, key, json.loads(values), operation="revise")
            txn = self._txn(row, proposed, "revise", why, identity, digest)
            self._manifest(proposed, manifest_text, txn["manifest_version_after"], txn["txn_id"])
            self.connection.execute(
                "UPDATE collections SET manifest_version = ?, audit_reader_version = 2 WHERE collection_id = ?",
                (txn["manifest_version_after"], current.collection_id),
            )
            after_container = self._advance(txn)
            payload = records.lifecycle_request_hash(
                action="revise",
                collection_id=current.collection_id,
                before_manifest_hash=current.manifest_version.hash,
                before_container_hash=self._container(row),
                proposed_manifest_hash=proposed.manifest_version.hash,
                acknowledged_gap_codes=(),
                rationale=why,
            )
            receipt = records._lifecycle_result(
                operation="revise",
                manifest=current,
                before_manifest_hash=current.manifest_version.hash,
                after_manifest_hash=proposed.manifest_version.hash,
                before_container_hash=self._container(row),
                after_container_hash=after_container,
                payload_hash=payload,
                audit_correlation=txn["transition_id"],
                continuity=True,
                acknowledged_gap_codes=(),
                gap_fingerprint=None,
                checkpoint_snapshot_hash=None,
                affected_paths=(current.path,),
            )
            governance_precommit_pending(self.connection, proposed, [current.path])
            self._insert_txn(txn, receipt)
        return receipt

    def discard_held(self, collection, *, held, why):
        records._validate_why(why)
        with self.handle.transaction():
            _, manifest, _ = self._collection(collection)
            candidate = self._held(manifest.collection_id, held)
            if candidate is None:
                raise records._held_not_found()
            governance_precommit_pending(self.connection, manifest, [candidate["view_path"]])
            self._release(candidate)
            receipt = {
                "_record_receipt": "exomem.records-mutation",
                "receipt_version": 1,
                "operation": "discard",
                "collection_id": manifest.collection_id,
                "held_id": candidate["held_id"],
                "affected_paths": [candidate["view_path"]],
                "outcome": "discarded",
                "audit_correlation": None,
            }
        return receipt
