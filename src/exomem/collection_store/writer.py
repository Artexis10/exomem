"""Preview-only built-in collection mutations, canonical in one SQLite transaction.

The caller owns and closes the lease-bound WriterConnection. Item, manifest and
held views publish after COMMIT; aggregate projections remain queued.
Canonical authorization precedes decoding current state and commits proposed state.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import sqlite3
import uuid
from collections import ChainMap
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any

from .. import (
    held_fs,
    memory_refs,
    mutation_terminal,
    planning,
    record_formats,
    records,
    vault,
    writer_lease,
)
from .. import structured_collections as collections
from ..governance.principal import effective_principal
from ..query_engine.indexes import IndexDeclarationError
from . import (
    chain,
    connection,
    governance,
    index_migrations,
    rollups,
    schema,
    summary,
    tables,
    tokens,
    typed_storage,
    types,
    views,
)
from .reader import row_source

BULK_UPSERT_MAX_ROWS = 500
#: The parent's items-mode row ceiling; summary mode is bounded by the store instead.
ITEMS_MODE_MAX_ROWS = 100_000


class _RecoveryOnly(Exception):
    """Unwind business work while allowing authorized foreign-input recovery to commit."""


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


def _renders_rows(manifest) -> bool:
    """Items-mode Markdown items get one view file per row; logs and summaries do not."""
    return manifest.storage.strategy == "markdown-items" and manifest.view_mode == "items"


def _affected(manifest, path) -> list[str]:
    """The views a row write changes: its file, the log, or the summary pages queued for reconcile."""
    if manifest.storage.strategy == "markdown-log":
        return [manifest.storage.source]
    if manifest.view_mode == summary.SUMMARY:
        return list(summary.page_paths(manifest))
    return [path]


def _refuse_view_stamp(manifest, values):
    if "exomem_view" in values or "exomem_view" in manifest.schema.fields:
        raise collections.CollectionError(
            "RESERVED_RECORD_FIELD", "item uses a reserved system field"
        )


class CollectionWriter:
    """Generic store writer parameterized by the persisted built-in declaration."""

    def __init__(self, vault_root: Path, handle: connection.WriterConnection) -> None:
        self.root = Path(vault_root)
        self.handle = handle
        self.connection = handle.connection
        self._operation = None
        self._facade_profile = None
        self._publication = None

    def _require_operation_context(self) -> None:
        self.handle.require_owner_thread()
        if self._operation is not None and self._operation.who != effective_principal():
            self._operation.refuse()

    @contextmanager
    def _authorization(self, *, mutation=True):
        self._require_operation_context()
        previous = self._operation
        if previous is not None:
            yield previous
            return
        operation = governance.OperationAuthorization(self.root, self.connection, mutation=mutation,
                                                       cache=self.handle.release_cache)
        self._operation = operation
        try:
            yield operation
        finally:
            operation.close()
            self._operation = None

    def _precommit(self, manifest):
        if self._publication is not None:
            self._publication.precommits[manifest.collection_id] = manifest
        self._operation.require_collection(manifest.collection_id, refresh=True)

    def _recheck_guard(self, guard, *, publication=False):
        self._publication.guards[id(guard)] = (guard, publication)
        try:
            guard.recheck(self.root)
        except vault.PathGuardError as error:
            if publication:
                raise records._publication_error(error) from error
            raise

    @contextmanager
    def _mutation(self, *, reconcile=False):
        batch = None
        recovery_only = False
        try:
            with self.handle.transaction(), self._authorization():
                if not reconcile and self.connection.execute(
                    "SELECT 1 FROM store_meta WHERE key=? AND value='1'", (schema.META_VIEW_DIVERGED,)
                ).fetchone():
                    raise connection.CollectionStoreError(
                        "COLLECTION_STORE_DIVERGED", "collection store requires reconciliation"
                    )
                batch = self._publication = views.PublicationBatch(self)
                self._row_counts = {}
                try:
                    yield
                except _RecoveryOnly:
                    recovery_only = True
                batch.stage_all()
                for manifest in tuple(batch.precommits.values()):
                    self._precommit(manifest)
                for guard, publication in tuple(batch.guards.values()):
                    self._recheck_guard(guard, publication=publication)
                if not reconcile and not recovery_only and (batch.foreign_discovered or self.connection.execute(
                    "SELECT 1 FROM store_meta WHERE key=? AND value='1'", (schema.META_VIEW_DIVERGED,)
                ).fetchone()):
                    raise connection.CollectionStoreError(
                        "COLLECTION_STORE_DIVERGED", "foreign view discovery requires reconciliation"
                    )
            batch.committed = True
            batch.publish()
            if recovery_only:
                raise connection.CollectionStoreError(
                    "COLLECTION_STORE_DIVERGED", "foreign view input was preserved; business mutation was refused"
                )
        except BaseException:
            if batch is not None and not batch.committed:
                batch.rollback()
            raise
        finally:
            self._publication = None
            if batch is not None:
                batch.close()

    def _preflight_views(self, paths):
        for path in paths:
            self._publication.capture_previous(path)
        if self._publication.foreign_discovered:
            if not self._publication.business_started and self.connection.execute(
                "SELECT 1 FROM store_meta WHERE key=? AND value='1'", (schema.META_VIEW_DIVERGED,)
            ).fetchone():
                raise _RecoveryOnly
            raise connection.CollectionStoreError(
                "COLLECTION_STORE_DIVERGED", "foreign view input remains preservation-pending"
            )

    def _execute(self, statement, parameters=(), *, many=False):
        """Account only this writer statement, never intervening trusted SQL."""
        before = self.connection.total_changes
        try:
            if not isinstance(statement, str):
                return self.handle.execute(statement, parameters or None)
            execute = self.connection.executemany if many else self.connection.execute
            return execute(statement, parameters)
        finally:
            self.handle.release_cache.account(self.connection.total_changes - before)

    def _collection_row(
        self, selector: str | Path | collections.CollectionManifest, *, facade_profile: str | None = None
    ):
        self._require_operation_context()
        raw = (
            selector.collection_id
            if isinstance(selector, collections.CollectionManifest)
            else str(selector)
        ).strip()
        if not raw:
            raise collections.CollectionError(
                "INVALID_COLLECTION_REFERENCE", "collection selector is required"
            )
        identity = memory_refs.parse_memory_ref(raw) or memory_refs.normalize_id(raw)
        field = "collection_id" if identity is not None else "manifest_path"
        key = identity if identity is not None else collections._reference_key(self.root, raw)
        if key is None:
            raise collections._spelling_reference_error(raw)
        row = _row(
            self.connection.execute(
                "SELECT c.* FROM collections c "
                f"WHERE c.{field} = ?",
                (key,),
            )
        )
        if row is None:
            from ..record_memory import _is_direct_legacy_tracker_selector

            if identity is None and not (
                facade_profile == "records" and _is_direct_legacy_tracker_selector(raw)
            ):
                raise collections._unresolvable_reference_error(key)
            raise collections.CollectionError("COLLECTION_NOT_FOUND", "collection was not found")
        return row

    def _collection(
        self, selector: str | Path | collections.CollectionManifest, *, facade_profile: str | None = None
    ):
        row = self._collection_row(selector, facade_profile=facade_profile)
        with self._authorization() as operation:
            operation.require_collection(row["collection_id"], complete=operation.mutation)
        manifest, declared = self._collection_manifest(row)
        profile = facade_profile or (self._facade_profile if operation.mutation else None)
        if profile is not None and not (profile == "records" and not operation.mutation) and manifest.semantic_profile != profile:
            raise collections.CollectionError(
                "PLANNING_PROFILE_REQUIRED" if profile == "planning" else "RECORDS_PROFILE_REQUIRED",
                f"{profile.title()} collection is required",
            )
        return row, manifest, declared

    def _collection_manifest(self, row):
        self._require_operation_context()
        cache = self.handle.release_cache
        cache.check()
        epoch = cache.epoch(row["collection_id"])
        key = (str(self.root), row["collection_id"], row["manifest_version"], epoch)
        if key in cache.contracts:
            manifest, declared = cache.contracts[key]
            return replace(manifest, audit_head=row["audit_head"]), declared
        text = self.connection.execute(
            "SELECT manifest_text FROM collection_manifests WHERE collection_id=? AND manifest_version=?",
            (row["collection_id"], row["manifest_version"]),
        ).fetchone()[0]
        manifest = collections.parse_manifest_bytes(
            self.root, row["manifest_path"], text.encode()
        )
        if manifest.collection_id != row["collection_id"] or manifest.manifest_version.hash != epoch[6]:
            governance.OperationAuthorization.refuse()
        declared = types.type_for_manifest(manifest)
        if (declared.name, declared.version) != (row["type_name"], row["type_version"]):
            raise types.CollectionTypeError(
                "COLLECTION_TYPE_VERSION_MISMATCH", "collection type differs from release"
            )
        cache._remember(cache.contracts, key, (manifest, declared), 64)
        return replace(manifest, audit_head=row["audit_head"]), declared

    def _item(self, cid: str, key: str) -> dict[str, Any] | None:
        row = _row(
            self.connection.execute(
                "SELECT * FROM items WHERE collection_id = ? AND item_key = ?", (cid, key)
            )
        )
        return None if row is None else typed_storage.hydrate(self.connection, [row])[0]

    def _container(self, row: Mapping[str, Any]) -> str:
        return tokens.container_hash(row["collection_id"], row["generation"], row["audit_head"])

    @contextmanager
    def read_snapshot(self):
        """Join canonical reads under one SQLite and policy snapshot."""
        self._require_operation_context()
        self.handle.release_cache.check()
        if self.connection.in_transaction:
            with self._authorization(mutation=False):
                yield
            return
        self.connection.execute("BEGIN")
        try:
            with self._authorization(mutation=False):
                yield
        finally:
            self.connection.execute("ROLLBACK")

    @contextmanager
    def read_collection(self, selector, *, facade_profile: str | None = None):
        """Resolve and read one collection under one SQLite and policy snapshot."""
        if facade_profile is None and isinstance(selector, collections.CollectionManifest):
            facade_profile = selector.semantic_profile
        with self.read_snapshot():
            manifest = self._collection(selector, facade_profile=facade_profile)[1]
            yield self._operation.field_plan(manifest).manifest

    def discover_collections(self, *, authorize_path=None, max_candidates=512, max_raw_candidates=512):
        from . import authority

        with self.read_snapshot():
            marker = authority.routing_marker(self)
            entries = ([(entry["collection_id"], entry["manifest_path"]) for entry in marker["collections"]]
                       if marker is not None else self.connection.execute(
                           "SELECT collection_id,manifest_path FROM collections ORDER BY manifest_path"
                       ).fetchall())
            manifests = []
            for cid, path in sorted(entries, key=lambda entry: entry[1]):
                if authorize_path is not None and not authorize_path(path):
                    continue
                if marker is not None:
                    authority.require_selected(self.connection, marker, authority.selected_entry(self.root, marker, cid),
                                               root=self.root)
                head = self._operation.summary_manifest(cid)
                if head is None:
                    head = (None, self._operation.decision(self._operation.catalog(cid)[0]))
                if head[1].level < 6:
                    continue
                if len(manifests) >= min(max_candidates, max_raw_candidates):
                    raise collections.CollectionError(
                        "COLLECTION_DISCOVERY_LIMIT", "too many collection manifests to inspect"
                    )
                manifest = self._collection(cid)[1]
                manifests.append(self._operation.field_plan(manifest).manifest)
            return tuple(manifests), ()

    def _projection_manifest(self, selector):
        """Canonical contract for server-internal write deltas; never egress."""
        with self.read_snapshot():
            return self._collection_manifest(self._collection_row(selector))[0]

    def _projection_snapshot(self, selector):
        """Audience-independent write-delta truth on the trusted writer binding."""
        from .reader import StoreAdapter

        with self.read_snapshot():
            row = self._collection_row(selector)
            manifest = self._collection_manifest(row)[0]
            cursor = self.connection.execute(
                "SELECT * FROM items WHERE collection_id=? ORDER BY view_path", (manifest.collection_id,),
            )
            names = [column[0] for column in cursor.description]
            items = typed_storage.hydrate(self.connection, [dict(zip(names, item, strict=True)) for item in cursor])
            return StoreAdapter(self, manifest, None)._snapshot(manifest, items, self._container(row))

    def _allows_item(self, selector, key):
        with self.read_snapshot():
            try:
                _, manifest, declared = self._collection(selector)
            except collections.CollectionError:
                return False
            identity = f"exomem://{declared.item_type}/{manifest.collection_id}/{key}"
            if manifest.view_mode == summary.SUMMARY:
                found = governance.subjects(self.connection, manifest.collection_id,
                                            self._operation.logical_vault_id, identity=identity)
                return bool(found) and self._operation.decision(found[0]).level >= 6
            return any(subject.basis.identity == identity and self._operation.decision(subject).level >= 6
                       for subject in self._operation.catalog(manifest.collection_id)[1:])

    def _projection_path_exists(self, path):
        """Canonical existence for delta pruning, without authorizing disclosure."""
        with self.read_snapshot():
            return self.connection.execute(
                "SELECT 1 FROM collections WHERE manifest_path=? OR source_path=? UNION ALL "
                "SELECT 1 FROM items WHERE view_path=? LIMIT 1", (path, path, path),
            ).fetchone() is not None

    def inspect_collection(self, collection, *, facade_profile: str | None = None) -> dict[str, Any]:
        """Report the dark writer's canonical state without publishing or repairing.

        This preview contract supports guard refresh and writer wire goldens.
        Production reader routing belongs to later slices.
        """
        self._require_operation_context()
        self.handle.release_cache.check()
        self.connection.execute("BEGIN")
        try:
            with self._authorization(mutation=False):
                result, selection = self._inspect_collection(collection, facade_profile=facade_profile)
                operation = self._operation
                from .field_admission import public_basis

                _, manifest, _ = self._collection(collection, facade_profile=facade_profile or self._facade_profile)
                field_basis = public_basis(operation, manifest)
                if field_basis is not None:
                    result["field_release_basis"] = field_basis
                evidence = operation.inspection_evidence(result, self.handle, selection)
                vault_id = operation.logical_vault_id
        finally:
            self.connection.execute("ROLLBACK")
        from ..governance import egress

        entries = ((subject.basis.identity, subject, subject.basis.payload_hash, decision)
                   for subject, decision in zip(selection.catalog, selection.notice_decisions, strict=True))
        notices = egress.canonical_subject_notices(
            self.root, entries, policy=operation.policy, principal=operation.who,
            purpose=operation.purpose,
            resolve_fingerprint=lambda subject: governance._bound(subject, vault_id).basis.fingerprint,
        )
        if notices:
            result["governance"] = {"notices": notices}
        return governance._seal_inspection_projection(result, evidence)

    def _inspect_collection(self, collection, *, facade_profile: str | None = None) -> tuple[dict[str, Any], governance._ReleaseSelection]:
        from .. import due_state, record_governance

        row, manifest, declared = self._collection(collection, facade_profile=facade_profile or self._facade_profile)
        selection = self._operation.inspection_selection(manifest.collection_id, notices=True)
        field_plan = self._operation.field_plan(manifest)
        if not field_plan.owner and (field_plan.whole_fields != set(manifest.schema.fields)):
            rows, snapshot, _ = self._operation.authorized_rows(manifest.collection_id)
            diagnostics = []
            record_governance._inspection_templates(
                self.root, field_plan.manifest, diagnostics, policy=self._operation.policy,
                authorize_path=self._operation.allows_file,
            )
            # Recipient inspection cannot publish full-state guards or import provenance; owners retain them below.
            return {"kind": "collection", "report_only": True,
                    "contract": record_governance._inspection_contract(field_plan.manifest),
                    "snapshot": snapshot,
                    "source_versions": [{"path": manifest.path, "hash": manifest.manifest_version.hash},
                                        *({"path": row_source(manifest, item), "hash": item["public_version"]}
                                          for item in rows[:record_governance._MAX_ITEM_ENTRIES - 1])],
                    "diagnostics": diagnostics,
                    "audit": None, "saved_views": [], "lifecycle_guards": {},
                    "coverage": {"committed": len(rows)}, "legacy": None}, selection
        allowed = selection.released
        basis = selection.inspection_basis
        release = self._operation.summary_release(manifest.collection_id)
        cached = None
        if release is None and basis is not None:
            epoch, profile = basis
            cached = self.handle.release_cache.inspections.inspect(
                manifest, epoch, profile, allowed,
                lambda subject: self._inspection_record(manifest, subject),
            )
        if release is not None:
            # Counts, guards and view states only: a summary inspection reads no row.
            versions = (manifest.manifest_version,)
            inspection = record_formats.CollectionInspection(
                collection_id=manifest.collection_id, snapshot=release.snapshot, source_versions=versions,
                source_hashes={version.path: version.hash for version in versions},
                diagnostics=record_formats.inspection_diagnostics(manifest, release.snapshot),
                record_count=release.released, presentation=(), observed_values=None,
            )
        elif cached is None:
            inspection = self._uncached_inspection(manifest)
        else:
            contributions, observed, presentation = cached
            per_row = _renders_rows(manifest)
            contributions = sorted(contributions, key=lambda contribution: (
                contribution.source.path if per_row else contribution.identity.key
            ))
            visible_snapshot = selection.snapshot
            source_versions = (manifest.manifest_version, *(
                contribution.source for contribution in contributions
                if per_row or manifest.storage.strategy == "markdown-log"))
            inspection = record_formats.CollectionInspection(
                collection_id=manifest.collection_id, snapshot=visible_snapshot,
                source_versions=source_versions,
                source_hashes={version.path: version.hash for version in source_versions},
                diagnostics=record_formats.inspection_diagnostics(manifest, visible_snapshot),
                record_count=len(contributions), presentation=presentation, observed_values=observed,
            )
        visible_snapshot = inspection.snapshot
        source_versions = inspection.source_versions
        allowed_rows = {subject.row_id for subject in allowed if isinstance(subject.row_id, int)}
        held_ids = {subject.row_id for subject in allowed if isinstance(subject.row_id, str)}
        diagnostics = record_governance._inspection_diagnostics(inspection.diagnostics)
        record_governance._inspection_templates(
            self.root, manifest, diagnostics, policy=self._operation.policy,
            authorize_path=self._operation.allows_file,
        )
        links = record_governance._LinkProjector.create(
            self.root, manifest, policy=self._operation.policy,
            authorize_path=self._operation.allows_file,
        )
        saved_views = record_governance._inspection_saved_views(self.root, manifest, links, diagnostics)
        catalog = selection.catalog
        if release is not None:
            complete, committed = release.complete, release.released
        else:
            complete = len(allowed_rows) == sum(isinstance(subject.row_id, int) for subject in catalog)
            committed = len(allowed_rows)
        held_paths = {subject.basis.subject.path for subject in catalog if subject.row_id in held_ids}
        pending = sum(
            kind == "manifest" or row_id in allowed_rows or (kind in ("log", "summary") and complete)
            or (kind == "held" and path in held_paths)
            for path, row_id, kind in self.connection.execute(
                "SELECT path,row_id,kind FROM projection_state WHERE collection_id=? AND state='pending'",
                (manifest.collection_id,),
            )
        )
        held = self.connection.execute(
            "SELECT held_id, updated_at, candidate_json, diagnostics_json, kind, code "
            f"FROM held_candidates WHERE collection_id = ? AND held_id IN ({','.join('?' for _ in held_ids)}) ORDER BY held_id",
            (manifest.collection_id, *sorted(held_ids)),
        ).fetchall()
        corrections = [candidate for candidate in held if candidate[4] != "write-refusal"]
        if pending:
            diagnostics.append({"code": "PROJECTION_PENDING", "reason": "collection views are pending publication"})
        for code in sorted({candidate[5] for candidate in corrections}):
            diagnostics.append({"code": code, "reason": "held collection view correction"})
        guards = {"expected_manifest_hash": manifest.manifest_version.hash,
                  "expected_container_hash": self._container(row) if complete else visible_snapshot}
        payload = {
            "kind": "collection", "report_only": True,
            "contract": record_governance._inspection_contract(manifest),
            "snapshot": visible_snapshot,
            "source_versions": [{"path": v.path, "hash": v.hash} for v in source_versions[:record_governance._MAX_ITEM_ENTRIES]],
            "diagnostics": diagnostics[:64],
            "audit": self._inspection_audit(row, complete=complete),
            "saved_views": saved_views, "lifecycle_guards": guards,
        }
        if manifest.view_mode == summary.SUMMARY:
            # Items mode stays wire-identical to file collections; summary names its own bounds.
            payload["contract"]["view_mode"] = manifest.view_mode
            payload["capacity"] = summary.capacity()
        if release is None and (manifest.item_presentation or manifest.record_presentation or manifest.item_filename):
            payload["presentation"] = record_governance._presentation_inspection(inspection.presentation, manifest)
        if declared.kind == "intended" and facade_profile != "records":
            payload["contract"].pop("plans")
            payload["contract"].pop("claims", None)
            return planning._project_inspection(payload, manifest), selection
        observations = due_state.collection_observation_coverage(
            self.root, manifest.path, authorize_path=self._operation.allows_file,
            now=dt.datetime.fromtimestamp(self._operation.now, dt.UTC),
        )
        payload.update({
            "legacy": None,
            # Summary inspection reads no row, so it has no value vocabulary to report.
            **({} if release is not None else {"observed_values": dict(inspection.observed_values or {})}),
            "coverage": {
                "committed": committed, "held": len(held), "unreadable": 0,
                "held_refs": [
                    {"held_id": candidate[0], "held_at": candidate[1],
                     "attempted_action": json.loads(candidate[2]).get("action", "update"),
                     "diagnostics": record_governance._diagnostics_summary(json.loads(candidate[3]))}
                    for candidate in held[:record_governance._HELD_REFERENCE_LIMIT]
                ],
                "unreflected": len(observations["unreflected"]),
                "unreflected_refs": list(observations["unreflected"])[:20],
                "pending": len(observations["pending"]),
                "pending_refs": list(observations["pending"])[:20],
                "state": "blocked" if held else "unknown" if not observations["complete"] else "partial" if observations["unreflected"] else "complete",
            },
            "projection": {"pending_views": pending, "held_view_corrections": len(corrections)},
        })
        return payload, selection

    def _inspection_audit(self, row, *, complete):
        from .. import record_governance

        incomplete = {"status": "history_incomplete", "gaps": []}
        if not complete:
            return incomplete
        if row["legacy_audit_status"] is None:
            return {"status": "ok" if row["audit_head"] else "baseline", "gaps": []}
        # Historical/deleted paths also gate imported gap topology and rationale.
        captured = self.connection.execute(
            "SELECT json_extract(receipt_json,'$.bounded_inspection'),"
            "json_extract(receipt_json,'$.bounded_inspection_paths') FROM txns "
            "WHERE collection_id=? AND txn_id=?",
            (row["collection_id"], row["created_txn"]),
        ).fetchone()
        try:
            paths = json.loads(captured[1]) if captured and captured[1] is not None else None
            if not isinstance(paths, list) or any(
                type(path) is not str or not self._operation.allows_history_path(path) for path in paths
            ):
                return incomplete
            audit = record_governance._inspection_audit(
                json.loads(captured[0]) if captured and captured[0] is not None else None
            )
        except (TypeError, ValueError):
            return incomplete
        return audit if audit["status"] == row["legacy_audit_status"] else incomplete

    def _inspection_record(self, manifest, subject):
        cursor = self.connection.execute(
            "SELECT row_id,encoding,collection_id,item_key,row_version,payload_hash,view_path,values_json "
            "FROM items WHERE collection_id=? AND row_id=?", (manifest.collection_id, subject.row_id),
        )
        item = _row(cursor)
        if item is None:
            governance.OperationAuthorization.refuse()
        typed_storage.hydrate(self.connection, [item])
        version = collections.SourceVersion(row_source(manifest, item), self._version(item))
        return record_formats.Record(
            collections.ItemIdentity(manifest.collection_id, item["item_key"]),
            json.loads(item["values_json"]), version, record_formats.SourceSpan(0, 0),
        )

    def _uncached_inspection(self, manifest):
        items, visible_snapshot, _ = self._operation.authorized_rows(manifest.collection_id)
        versions = [manifest.manifest_version]
        parsed = []
        for item in items:
            version = collections.SourceVersion(row_source(manifest, item), self._version(item))
            if manifest.view_mode == "items":
                versions.append(version)
            parsed.append(record_formats.Record(
                collections.ItemIdentity(manifest.collection_id, item["item_key"]),
                json.loads(item["values_json"]), version, record_formats.SourceSpan(0, 0),
                body=item["body"],
            ))
        snapshot = record_formats.AdapterSnapshot(
            records=tuple(parsed), snapshot=visible_snapshot, data_snapshot=visible_snapshot,
            source_versions=tuple(versions),
        )
        return record_formats.inspect_collection(self.root, manifest, snapshot=snapshot)

    def _version(self, row: Mapping[str, Any]) -> str:
        return tokens.item_version(
            row["collection_id"], row["item_key"], row["row_version"], row["payload_hash"]
        )

    def _identity(
        self, action: str, selector: Any, args: Mapping[str, Any], request_id: str | None
    ):
        if action != "create":
            self._collection(selector)
        else:
            existing = self.connection.execute("SELECT collection_id FROM collections WHERE manifest_path=?",
                                               (collections._reference_key(self.root, str(selector)),)).fetchone()
            if existing is not None:
                self._collection(existing[0])
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
                "SELECT request_hash, receipt_json, collection_id FROM txns WHERE request_id = ?", (identity,)
            ).fetchone()
            if stored is not None:
                self._operation.require_collection(stored[2])
                if stored[0] != digest:
                    raise collections.CollectionError(
                        "IDEMPOTENCY_KEY_REUSED",
                        "request identity already names different arguments",
                    )
                return identity, digest, mutation_terminal._CanonicalRequestReplay(json.loads(stored[1]))
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
        control: bool = False,
    ):
        """One chained txn; a ``control`` txn keeps the collection's generation and manifest."""
        self._publication.business_started = True
        seq, previous = chain.recorded_head(self.connection)
        txn_id = self.connection.execute(
            "SELECT COALESCE(MAX(txn_id), 0) + 1 FROM txns"
        ).fetchone()[0]
        generation = 0 if row is None else row["generation"]
        before_manifest = None if row is None else row["manifest_version"]
        after_manifest = 1 if row is None else before_manifest + (operation == "revise")
        if control:
            after_manifest = before_manifest
        transition = records._transition_id()
        committed_at = dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        event = {
            "transition_id": transition,
            "collection_id": manifest.collection_id,
            "operation": operation,
            "generation_before": generation,
            "generation_after": generation + (not control),
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
            if manifest.semantic_profile == "planning" and not control
            else None,
            "generation_before": generation,
            "generation_after": generation + (not control),
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
        valid = mutation_terminal.valid_collection_receipt(receipt)
        if txn["operation"] in mutation_terminal.CONTROL_OPERATIONS:
            valid = (
                mutation_terminal.valid_control_receipt(receipt)
                and receipt["operation"] == txn["operation"]
                and (receipt["collection_id"], receipt["transition_id"], receipt["commit_seq"])
                == (txn["collection_id"], txn["transition_id"], txn["commit_seq"])
                and txn["generation_before"] == txn["generation_after"]
            )
        elif txn["operation"] == "bulk_upsert":
            valid = (
                receipt["operation"] == "bulk_upsert" and receipt["committed"] is True
                and receipt["collection_id"] == txn["collection_id"]
                and receipt["first_transition"] == receipt["last_transition"] == txn["transition_id"]
                and all(row.get("transition_id") == txn["transition_id"]
                        for row in receipt["rows"] if row["outcome"] in {"inserted", "updated"})
            )
        if not valid:
            raise RuntimeError("writer constructed an invalid collection receipt")
        data = {**txn, "receipt_json": _json(receipt)}
        self._execute(tables.INSERT_TXN, data)
        if self._publication.deferred_create is None and txn["operation"] not in mutation_terminal.CONTROL_OPERATIONS:
            self._publication.bind(receipt)

    def _advance(self, txn: Mapping[str, Any]) -> str:
        self._execute(
            tables.ADVANCE,
            {"generation": txn["generation_after"], "audit_head": txn["event_hash"],
             "updated_txn": txn["txn_id"], "target_collection_id": txn["collection_id"]},
        )
        return tokens.container_hash(
            txn["collection_id"], txn["generation_after"], txn["event_hash"]
        )

    def _pending(
        self, path: str, cid: str, kind: str, version: int, text: str, row_id: int | None = None,
        *, manifest=None,
    ) -> None:
        descriptor = None
        digest = hashlib.sha256(text.encode()).hexdigest()
        # nosemgrep: ep-word-membership -- The projection_state.kind CHECK fixes these kinds.
        if kind in {"manifest", "item", "held", "summary"}:
            descriptor = self._publication.prepare({
                "path": path, "collection_id": cid, "kind": kind, "row_id": row_id,
                "pending_row_version": version, "pending_sha256": digest,
            }, self._publication.previous(path), manifest)
        self._execute(tables.PENDING, {
            "path": path, "collection_id": cid, "row_id": row_id, "kind": kind,
            "pending_row_version": version, "pending_sha256": digest, "state": "pending",
            "install_json": descriptor,
        })

    def _render_view(self, projection, manifest):
        """Render indexed canonical rows using a context prepared before the filesystem phase."""
        return views.render_view(self.connection, self._publication.identity, projection, manifest)

    def _classify_view_input(self, projection, raw):
        try:
            frontmatter, _, _ = vault.parse_frontmatter(raw.decode(), strict=True)
            view_stamp = frontmatter.get("exomem_view")
        except (UnicodeError, ValueError):
            view_stamp = None
        if not isinstance(view_stamp, dict):
            return "VIEW_INVALID", None, None
        if (set(view_stamp) != {"s", "i", "v", "h"} or
                any(memory_refs.normalize_id(view_stamp.get(name)) is None for name in ("s", "i")) or
                type(view_stamp.get("v")) is not int or view_stamp["v"] < 1 or
                not isinstance(view_stamp.get("h"), str) or len(view_stamp["h"]) != 12 or
                any(char not in "0123456789abcdef" for char in view_stamp["h"])):
            return "VIEW_INVALID", None, None
        lineage = json.loads(self.connection.execute(
            "SELECT value FROM store_meta WHERE key='lineage'"
        ).fetchone()[0])
        if (view_stamp.get("s") != self._publication.identity["store_id"] or
                view_stamp.get("i") not in {entry["instance_id"] for entry in lineage}):
            return "VIEW_FOREIGN", view_stamp, None
        if projection["kind"] == "item":
            current = self.connection.execute(
                "SELECT row_version,payload_hash FROM items WHERE row_id=?", (projection["row_id"],)
            ).fetchone()
        elif projection["kind"] == "manifest":
            current = self.connection.execute(
                "SELECT c.manifest_version,m.manifest_hash FROM collections c JOIN collection_manifests m "
                "ON m.collection_id=c.collection_id AND m.manifest_version=c.manifest_version WHERE c.collection_id=?",
                (projection["collection_id"],),
            ).fetchone()
        elif projection["kind"] == "summary":
            # Generated read-only output: hold any edit with its bytes, never parse it into rows.
            generation = self.connection.execute(
                "SELECT generation FROM collections WHERE collection_id=?", (projection["collection_id"],)
            ).fetchone()[0]
            return "SUMMARY_VIEW_READ_ONLY", view_stamp, generation
        else:
            return "VIEW_INVALID", view_stamp, projection["pending_row_version"]
        version, payload = current
        base = view_stamp.get("v")
        if isinstance(base, int) and not isinstance(base, bool) and base < version:
            return "VIEW_CONFLICT", view_stamp, version
        if base != version or view_stamp.get("h") != payload[:12]:
            return "VIEW_INVALID", view_stamp, version
        try:
            _, manifest, declared = self._collection(projection["collection_id"])
            if projection["kind"] == "item":
                key, schema_version = self.connection.execute(
                    "SELECT item_key,schema_version FROM items WHERE row_id=?", (projection["row_id"],)
                ).fetchone()
                profile = record_formats.profile_for(manifest.semantic_profile)
                system = {"type": profile.item_type, "collection_id": manifest.collection_id,
                          profile.item_id_property: key, "schema_version": schema_version}
                if any(frontmatter.get(name) != value for name, value in system.items()):
                    return "VIEW_INVALID", view_stamp, version
                values = {name: value for name, value in frontmatter.items()
                          if name not in {*system, "exomem_view"}}
                self._validate(manifest, declared, key, values, operation="update", validate_graph=False)
            else:
                proposed = collections.parse_manifest_bytes(self.root, projection["path"], raw)
                if proposed.view_mode != manifest.view_mode and summary.populated(
                        self.connection, manifest.collection_id):
                    return summary.MODE_CHANGE_UNSUPPORTED, view_stamp, version
        except collections.CollectionError:
            return "VIEW_INVALID", view_stamp, version
        # Current-base edit-back is a later P2 slice; retain its bytes and ownership.
        return None, view_stamp, version

    def _register_view_input(self, projection, raw, token, slot, *, source=None):
        code, view_stamp, current = self._classify_view_input(projection, raw)
        if code == "VIEW_FOREIGN":
            self._publication.foreign_discovered = True
        if code is None:
            self._publication.pending = True
            return False
        reference = hashlib.sha256(f"{projection['path']}\0{token}\0{slot}\0".encode() + raw).hexdigest()[:24]
        existing = self.connection.execute(
            "SELECT held_bytes FROM held_candidates WHERE held_id=?", (reference,)
        ).fetchone()
        if existing is not None:
            return existing[0] == raw
        _, manifest, _ = self._collection(projection["collection_id"])
        path = f"{records._held_directory(manifest)}/{reference}.md"
        diagnostics = [{
            "code": code, "path": projection["path"], "token": token, "slot": slot,
            "sha256": hashlib.sha256(raw).hexdigest(), "stamp": view_stamp,
            "base_row_version": view_stamp.get("v") if view_stamp else None,
            "current_row_version": current,
        }]
        contexts = (source or {}).get("source_contexts", [])
        pairs = {tuple(pair) for name in ("previous_published", "previous_pending")
                 if (pair := (source or {}).get(name)) is not None}
        metadata = set()
        covered = set()
        try:
            for context in contexts:
                pair = (context.get("row_version"), context.get("sha256"))
                if pair not in pairs or any(context.get(name) != projection.get(name)
                                           for name in ("path", "collection_id", "kind", "row_id")):
                    continue
                metadata.add(_json(governance._metadata(context["governance_json"])))
                covered.add(pair)
        except (KeyError, TypeError, ValueError):
            metadata.clear()
        if not pairs or covered != pairs or len(metadata) != 1:
            self._publication.pending = True
            return False
        metadata = metadata.pop()
        now = dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        self._publication.capture_previous(path)
        self._execute(
            "INSERT INTO held_candidates(held_id,collection_id,kind,code,candidate_json,held_bytes,"
            "diagnostics_json,view_path,base_row_version,updated_at,governance_json,governance_hash) "
            "VALUES (?,?,'view-correction',?,?,?,?,?,?,?,?,?)",
            (reference, projection["collection_id"], code, _json({"action": "view-edit"}), raw,
             _json(diagnostics), path, view_stamp.get("v") if view_stamp and type(view_stamp.get("v")) is int else None, now,
             metadata, hashlib.sha256(raw).hexdigest()),
        )
        text = views.held_view({
            "collection_id": projection["collection_id"], "held_id": reference,
            "updated_at": now, "kind": "view-correction", "held_bytes": raw,
            "diagnostics_json": _json(diagnostics),
            "governance_json": metadata,
        }, self._publication.identity)
        self._pending(path, projection["collection_id"], "held", 1, text, manifest=manifest)
        self.handle.release_cache.touch(projection["collection_id"],
                                       f"exomem://collection-held/{projection['collection_id']}/{reference}")
        if code == "VIEW_FOREIGN":
            self._execute("INSERT OR REPLACE INTO store_meta(key,value) VALUES (?,'1')", (schema.META_VIEW_DIVERGED,))
        self._precommit(manifest)
        return True

    def _recover_view_inputs(self, batch, previous):
        descriptor = json.loads(previous["install_json"])
        parent = batch._parent(previous["path"])
        preserved = []
        for displaced in [descriptor, *descriptor.get("preserved", [])]:
            expected = {pair[1] for name in ("previous_published", "previous_pending")
                        if (pair := displaced.get(name)) is not None}
            found = False
            for slot in range(2):
                leaf = views.ASIDE_PREFIX + displaced["token"] + f"-{slot}"
                result = batch._fs().file(parent, leaf)
                if result.error is not None and result.error.code == "MISSING":
                    continue
                with result.require() as file:
                    raw = batch._fs().read(file).require()
                found = True
                digest = hashlib.sha256(raw).hexdigest()
                # Keep token ownership through cleanup, including an editor changing the aside again.
                if digest not in expected and not self._register_view_input(previous, raw, displaced["token"], slot, source=displaced):
                    raise connection.CollectionStoreError(
                        "COLLECTION_STORE_PROJECTION_PENDING", "view edit awaits governed edit-back"
                    )
                batch.cleanup.append((previous, parent, leaf, digest))
            if found:
                preserved.append({name: value for name, value in displaced.items() if name != "preserved"})
        stage_leaf = views.STAGE_PREFIX + descriptor["token"]
        result = batch._fs().file(parent, stage_leaf)
        if result.ok:
            with result.require() as file:
                raw = batch._fs().read(file).require()
                link_count = file.identity.link_count
            digest = hashlib.sha256(raw).hexdigest()
            if digest == previous["pending_sha256"]:
                if link_count == 2:
                    batch.pair_cleanup[previous["path"]] = (parent, stage_leaf, digest)
                else:
                    batch.cleanup.append((previous, parent, stage_leaf, digest))
        elif result.error.code != "MISSING":
            raise result.error
        return preserved

    def reconcile_views(self, *, limit=64, after=None, pending_only=False):
        """Recover one path window; inspect never writes or invokes this path.

        The window does not bound file bytes, preserved inputs or directory enumeration.
        ``pending_only`` takes only views awaiting publication, as the serving store thread does.
        """
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 1024:
            raise ValueError("reconcile limit must be between 1 and 1024")
        if after is not None and not isinstance(after, str):
            raise ValueError("reconcile continuation must be a path")
        result = {"examined": 0, "warnings": [], "next_path": None}
        with self._mutation(reconcile=True):
            batch = self._publication
            batch.bind(result)
            result["recovered_stages"] = batch.recovered_stages
            cursor = self.connection.execute(
                "SELECT * FROM projection_state WHERE kind IN ('item','manifest','held','summary') AND path>? "
                "AND (? OR state='pending') ORDER BY path LIMIT ?",
                (after or "", not pending_only, limit + 1),
            )
            rows = [dict(zip((c[0] for c in cursor.description), row, strict=True)) for row in cursor.fetchall()]
            if len(rows) > limit:
                rows = rows[:limit]
                result["next_path"] = rows[-1]["path"]
            visited_parents = set()
            for projection in rows:
                if not batch.eligible(projection):
                    continue
                _, manifest, _ = self._collection(projection["collection_id"])
                batch.precommits[manifest.collection_id] = manifest
                result["examined"] += 1
                try:
                    parent_path = Path(projection["path"]).parent
                    if parent_path not in visited_parents:
                        batch._parent(projection["path"], create=True)
                        batch.recover_orphans(projection["path"], limit=limit)
                        visited_parents.add(parent_path)
                    projection = batch.capture_previous(projection["path"])
                    preserved = batch.recovered_inputs[projection["path"]]
                    if preserved is None:
                        continue
                    parent = batch._parent(projection["path"])
                    opened = batch._fs().file(parent, Path(projection["path"]).name)
                    raw = None
                    if opened.ok:
                        with opened.require() as file:
                            raw = batch._fs().read(file).require()
                    elif opened.error.code != "MISSING":
                        raise opened.error
                    digest = hashlib.sha256(raw).hexdigest() if raw is not None else None
                    if digest is not None and digest == projection["pending_sha256"]:
                        if not preserved:
                            self._execute(
                                "UPDATE projection_state SET published_row_version=pending_row_version,"
                                "published_sha256=pending_sha256,pending_row_version=NULL,pending_sha256=NULL,"
                                "state='current' WHERE path=?", (projection["path"],)
                            )
                        continue
                    if digest is not None and digest == projection["published_sha256"] and projection["state"] == "current":
                        continue
                    if raw is not None and digest not in {projection["published_sha256"], projection["pending_sha256"]}:
                        token = json.loads(projection["install_json"])["token"] if projection["install_json"] else uuid.uuid4().hex
                        source = {
                            "previous_published": views._pair(projection["published_row_version"], projection["published_sha256"]),
                            "previous_pending": views._pair(projection["pending_row_version"], projection["pending_sha256"]),
                            "source_contexts": projection["source_contexts"],
                        }
                        if not self._register_view_input(projection, raw, token, "offline", source=source):
                            batch.pending = True
                            descriptor = json.loads(projection["install_json"]) if projection["install_json"] else {
                                "token": token, "previous_published": views._pair(projection["published_row_version"], projection["published_sha256"]),
                                "previous_pending": views._pair(projection["pending_row_version"], projection["pending_sha256"]),
                            }
                            descriptor["deferred_target_sha256"] = digest
                            self._execute("UPDATE projection_state SET state='pending',install_json=? WHERE path=?",
                                          (_json(descriptor), projection["path"]))
                            continue
                    if projection["kind"] == "held":
                        retired = _row(self.connection.execute("SELECT * FROM held_candidates WHERE held_id=? AND view_path=?",
                                                              (Path(projection["path"]).stem, projection["path"]))) is None
                    else:
                        # A page its collection no longer declares (an empty collection left summary mode).
                        retired = projection["kind"] == "summary" and projection["path"] not in summary.page_paths(manifest)
                    if retired:
                        if raw is None and not preserved:
                            self._execute("DELETE FROM projection_state WHERE path=?", (projection["path"],))
                        else:
                            batch.retire(projection)
                        continue
                    version, text = self._render_view(projection, manifest)
                    self._pending(projection["path"], projection["collection_id"], projection["kind"], version, text, projection["row_id"], manifest=manifest)
                except (held_fs.HeldFsError, OSError, connection.CollectionStoreError):
                    batch.pending = True
                self._precommit(manifest)
        return result

    def record_control_transition(self, operation, transitions, *, why):
        """Record a content-free control transition: one chained txn per affected collection.

        ``transitions`` maps each collection id to ``{"counts": {name: int}, "ids":
        {name: [id, ...]}}``: counts and identifiers only, never item values. Each txn
        advances ``commit_seq`` and the store head, so the next replica publication and
        an orderly release carry it, and leaves the collection's generation, manifest and
        container hash unchanged. Inside an open writer mutation it joins that SQLite
        transaction; otherwise it opens its own. Returns the receipts in collection order.

        A collection whose ids exceed ``CONTROL_RECEIPT_MAX_IDS`` is recorded as
        successive txns that split the ids in order; each of those receipts repeats the
        counts and adds ``part`` and ``parts`` (1-based).
        """
        if operation not in mutation_terminal.CONTROL_OPERATIONS:
            raise ValueError(f"unknown control operation {operation!r}")
        if not isinstance(why, str) or not why.strip():
            raise ValueError("a control transition needs a reason")
        if self._publication is None:
            with self._mutation():
                return self.record_control_transition(operation, transitions, why=why)
        limit, receipts = mutation_terminal.CONTROL_RECEIPT_MAX_IDS, []
        for cid in sorted(transitions):
            row, manifest, _ = self._collection(cid)
            counts = dict(transitions[cid].get("counts", {}))
            flat = [(name, found) for name, ids in transitions[cid].get("ids", {}).items() for found in ids]
            slices = [flat[start:start + limit] for start in range(0, len(flat), limit)] or [[]]
            for part, chunk in enumerate(slices, 1):
                ids = {name: [] for name in transitions[cid].get("ids", {})}
                for name, found in chunk:
                    ids[name].append(found)
                facts = {"counts": counts if len(slices) == 1 else {**counts, "part": part, "parts": len(slices)},
                         "ids": ids}
                txn = self._txn(row, manifest, operation, why, None, None, effects=facts, control=True)
                receipt = {"_control_receipt": mutation_terminal.CONTROL_RECEIPT_MARKER, "receipt_version": 1,
                           "operation": operation, "collection_id": cid, "transition_id": txn["transition_id"],
                           "commit_seq": txn["commit_seq"], **facts, "outcome": "committed"}
                self._insert_txn(txn, receipt)
                receipts.append(receipt)
            self._precommit(manifest)
        return receipts

    def hold_store_delta(self, items, *, reconciled, why, acknowledged=()):
        """Divergence reconciliation (design §15 item 5): each foreign change becomes a held correction.

        No canonical row changes. Each item another store instance changed after the
        common ancestor is held as a view correction carrying that store's latest values
        and diagnostics for an owner decision under the plan's ``held_id``, superseding an
        older hold. The plan carries one latest change per held id and leaves out a change
        already held, so a repeat with nothing new records nothing.
        The ``reconciled`` evidence digests are marked, a view-stamp divergence is cleared,
        and the reconciliation is recorded as one content-free control transition per
        affected collection. ``acknowledged`` are the preview's skipped changes the owner
        accepted; their ids are recorded by skip code.
        """
        result = {"held_ids": [], "superseded": 0, "why": why}
        skipped = {}
        for entry in acknowledged:
            ref = entry["sha256"] if "item_key" not in entry else f"{entry['collection_id']}:{entry['item_key']}"
            skipped.setdefault(f"skipped_{entry['code']}", []).append(ref)
        with self._mutation(reconcile=True):
            self._publication.bind(result)
            touched = {}
            for item in items:
                cid, key = item["collection_id"], item["item_key"]
                _, manifest, _ = self._collection(cid)
                reference = item["held_id"]
                path = f"{records._held_directory(manifest)}/{reference}.md"
                self._preflight_views([path])
                raw = _json({"item_key": key, "values": item["values"], "body": item["body"]}).encode()
                metadata = self.connection.execute(
                    "SELECT m.governance_json FROM collection_manifests m JOIN collections c ON "
                    "c.collection_id=m.collection_id AND c.manifest_version=m.manifest_version WHERE c.collection_id=?",
                    (cid,),
                ).fetchone()[0]
                try:
                    held_metadata = governance.row_metadata(manifest.schema, item["values"], metadata)
                except (ValueError, collections.CollectionError):
                    held_metadata = _json({**governance._metadata(metadata), "tags": [], "classes": []})
                diagnostics = _json([{"code": "COLLECTION_STORE_DIVERGED", **{
                    name: value for name, value in item.items() if name not in {"values", "body"}}}])
                now = dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
                superseded = self.connection.execute(
                    "SELECT 1 FROM held_candidates WHERE held_id=?", (reference,)).fetchone() is not None
                self._execute(
                    "INSERT INTO held_candidates(held_id,collection_id,kind,code,candidate_json,held_bytes,"
                    "diagnostics_json,view_path,base_row_version,updated_at,governance_json,governance_hash) "
                    "VALUES (?,?,'view-correction','COLLECTION_STORE_DIVERGED',?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(held_id) DO UPDATE SET candidate_json=excluded.candidate_json,"
                    "held_bytes=excluded.held_bytes,diagnostics_json=excluded.diagnostics_json,"
                    "base_row_version=excluded.base_row_version,updated_at=excluded.updated_at,"
                    "governance_json=excluded.governance_json,governance_hash=excluded.governance_hash",
                    (reference, cid, _json({"action": "store-delta", "item_key": key}), raw, diagnostics, path,
                     item["local_row_version"], now, held_metadata, hashlib.sha256(raw).hexdigest()),
                )
                text = views.held_view({
                    "collection_id": cid, "held_id": reference, "updated_at": now, "kind": "view-correction",
                    "held_bytes": raw, "diagnostics_json": diagnostics, "governance_json": held_metadata,
                }, self._publication.identity)
                self._pending(path, cid, "held", 1, text, manifest=manifest)
                self.handle.release_cache.touch(cid, f"exomem://collection-held/{cid}/{reference}")
                self._precommit(manifest)
                facts = touched.setdefault(cid, {"held": [], "superseded": 0})
                facts["held"].append(reference)
                facts["superseded"] += superseded
                result["held_ids"].append(reference)
                result["superseded"] += superseded
            meta = dict(self.connection.execute(
                "SELECT key,value FROM store_meta WHERE key IN (?,?)",
                (schema.META_RECONCILED_FOREIGN, schema.META_VIEW_DIVERGED)))
            done = json.loads(meta.get(schema.META_RECONCILED_FOREIGN) or "[]")
            marked = sorted(set(reconciled) - set(done))
            cleared = schema.META_VIEW_DIVERGED in meta
            if marked:
                self._execute("INSERT INTO store_meta(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET "
                              "value=excluded.value", (schema.META_RECONCILED_FOREIGN, _json(sorted({*done, *marked}))))
            if cleared:
                self._execute("DELETE FROM store_meta WHERE key=?", (schema.META_VIEW_DIVERGED,))
            result.update(reconciled=marked, view_divergence_cleared=cleared, transitions=[])
            if touched or marked or cleared:
                # Store-wide marks without a hold touch every collection's history.
                affected = touched or {cid: {"held": [], "superseded": 0} for (cid,) in self.connection.execute(
                    "SELECT collection_id FROM collections ORDER BY collection_id").fetchall()}
                result["transitions"] = self.record_control_transition("store_reconcile", {
                    cid: {"counts": {"held": len(facts["held"]), "superseded": facts["superseded"],
                                     "evidence_reconciled": len(marked), "view_divergence_cleared": int(cleared),
                                     "skipped_acknowledged": len(acknowledged)},
                          "ids": {"held_ids": facts["held"], "evidence_sha256": marked, **skipped}}
                    for cid, facts in affected.items()}, why=why)
        return result

    def backfill_derived(self, collection, *, limit=128, projection=True, rollup=True) -> tuple[bool, bool]:
        """Advance this collection's query projection and building rollups by one batch each, in one transaction.

        Internal maintenance under the writer lease and current authority, not a
        public route; the serving store thread drives it. A collection with no
        projection at all starts its first build here. Canonical items,
        generations and audit are unchanged. Returns whether the projection
        published (True only on the batch that publishes it) and whether every
        declared rollup is ready; until then the planner reads base rows.
        """
        with self._mutation():
            _, manifest, declared = self._collection(collection)
            accounted = index_migrations.AccountedWriter(self.connection, self._execute)
            published = ready = False
            if projection:
                index_migrations.begin_missing(accounted, manifest.collection_id, declared)
                published = index_migrations.backfill_batch(accounted, manifest.collection_id, limit=limit)
            if rollup:
                ready = rollups.backfill_batch(accounted, manifest.collection_id, limit=limit)
            self._precommit(manifest)
        return published, ready

    def backfill_query_indexes(self, collection, *, limit=128) -> bool:
        """One projection batch alone; True when it publishes the projection."""
        return self.backfill_derived(collection, limit=limit, rollup=False)[0]

    def backfill_rollups(self, collection, *, limit=128) -> bool:
        """One rollup batch alone; True once every declared rollup is ready."""
        return self.backfill_derived(collection, limit=limit, projection=False)[1]

    def migrate_typed_encoding(self, collection, *, limit=128) -> str:
        """Advance this collection's forward typed-v1 encoding migration by one batch.

        Internal maintenance under the writer lease and current authority, not
        a public route. Logical values, hashes, generations and audit are
        unchanged; the JSON encoding stays authoritative until the proved
        cutover commits. Returns ``building``, ``ready`` or ``failed``.
        """
        with self._mutation():
            _, manifest, _ = self._collection(collection)
            state = typed_storage.migrate_batch(
                index_migrations.AccountedWriter(self.connection, self._execute),
                manifest.collection_id, tuple(manifest.schema.fields), limit=limit,
            )
            self._precommit(manifest)
        return state

    def _manifest(
        self, manifest: collections.CollectionManifest, text: str, version: int, txn_id: int
    ):
        self._publication.capture_previous(manifest.path)
        data, _, _ = vault.parse_frontmatter(text, strict=True)
        try:
            index_migrations.prepare_manifest(index_migrations.AccountedWriter(self.connection, self._execute),
                                               manifest, data, types.type_for_manifest(manifest))
        except IndexDeclarationError as error:
            raise collections.CollectionError(error.code, error.reason) from error
        self._execute(
            "INSERT INTO collection_manifests(collection_id,manifest_version,manifest_text,manifest_hash,"
            "schema_json,natural_key_json,txn_id,governance_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                manifest.collection_id,
                version,
                text,
                manifest.manifest_version.hash,
                _json(data["item_schema"]),
                _json(list(manifest.schema.natural_key)),
                txn_id,
                governance.manifest_metadata(text),
            ),
        )
        rendered = views.manifest_view(
            text, views.stamp(self._publication.identity, version, manifest.manifest_version.hash)
        )
        self._pending(manifest.path, manifest.collection_id, "manifest", version, rendered, manifest=manifest)
        self.handle.release_cache.touch(manifest.collection_id)

    def create_collection(
        self,
        manifest_path: str | Path,
        manifest_text: str,
        *,
        why: str,
        scaffold: bool = True,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        return self._create_collection(manifest_path, manifest_text, why=why,
                                       scaffold=scaffold, request_id=request_id)

    def _create_collection(self, manifest_path, manifest_text, *, why, scaffold,
                           request_id, admission=None):
        records._validate_why(why)
        with self._mutation():
            identity, digest, replay = self._identity(
                "create",
                manifest_path,
                {"manifest_text": manifest_text, "why": why, "scaffold": scaffold},
                request_id,
            )
            if admission is not None:
                admission.check(identity, digest, replay)
            if replay is not None:
                return replay
            manifest = collections.parse_manifest_bytes(
                self.root, manifest_path, manifest_text.encode()
            )
            declared = types.type_for_manifest(manifest)
            if self._facade_profile is not None and manifest.semantic_profile != self._facade_profile:
                raise collections.CollectionError(
                    "PLANNING_PROFILE_REQUIRED" if self._facade_profile == "planning" else "RECORDS_PROFILE_REQUIRED",
                    f"{self._facade_profile.title()} collection is required",
                )
            if self._facade_profile == "planning" and scaffold:
                manifest_text = planning._with_default_scaffold(
                    manifest_text, manifest, vault_root=self.root
                )
                manifest = collections.parse_manifest_bytes(self.root, manifest_path, manifest_text.encode())
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
            conflicts = self.connection.execute(
                "SELECT collection_id FROM collections WHERE collection_id = ? OR manifest_path = ? OR source_path = ?",
                (manifest.collection_id, manifest.path, manifest.storage.source),
            ).fetchall()
            if conflicts:
                for (cid,) in conflicts:
                    self._operation.require_collection(cid)
                raise collections.CollectionError(
                    "CREATE_ONLY_CONFLICT", "collection manifest already exists"
                )
            # File-mode data cannot be silently adopted by a dark writer.
            records._assert_portable_absent(self.root, self.root / manifest.path)
            records._assert_portable_absent(self.root, self.root / manifest.storage.source)
            txn = self._txn(None, manifest, "create", why, identity, digest)
            if admission is not None:
                admission.record(manifest, txn)
            types.register_builtins(self.connection, txn_id=txn["txn_id"])
            self._execute(
                "INSERT INTO collections (collection_id, type_name, type_version, manifest_path, source_path, layout, "
                "manifest_version, generation, audit_head, audit_reader_version, created_txn, updated_txn, view_mode) "
                "VALUES (?, ?, ?, ?, ?, ?, 1, 1, ?, 1, ?, ?, ?)",
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
                    manifest.view_mode,
                ),
            )
            self._manifest(manifest, manifest_text, 1, txn["txn_id"])
            if manifest.view_mode == summary.SUMMARY:
                # A NEW summary collection is typed-v1 from its first row: an empty
                # forward migration publishes the layout in this transaction.
                if typed_storage.migrate_batch(index_migrations.AccountedWriter(self.connection, self._execute),
                                               manifest.collection_id, tuple(manifest.schema.fields), limit=1) != "ready":
                    raise RuntimeError("an empty collection publishes its typed layout at once")
                self._pending_summary(manifest, 1)
            affected = [manifest.path, *summary.page_paths(manifest)] + ([manifest.storage.source] if scaffold else [])
            log_text = None
            if scaffold and manifest.storage.strategy == "markdown-log":
                section = manifest.storage.descriptor["section"]
                log_text = f"{'#' * section['level']} {section['title']}\n"
                self._execute(
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
            self._precommit(manifest)
            self._insert_txn(txn, receipt)
        writer_lease.mark_active_mutation_committed()
        return receipt

    def _validate(
        self, manifest, declared, key, values, *, before=None, operation="append",
        validate_graph=True,
    ):
        _refuse_view_stamp(manifest, values)
        if declared.kind == "intended":
            planning.require_planning_profile(manifest)
            values = planning.normalize_item(
                values,
                vault_root=self.root,
                # A revision re-checks stored values against the proposed manifest.
                stored=values if operation == "revise" else before,
                apply_defaults=operation == "append",
                validate_motivation=planning.motivation_is_governed(manifest),
            )
            planning._validate_declared_text(manifest, values)
            if before is not None:
                planning._require_same_area_side(before, values)
        values = records._validate_values(manifest, values)
        if declared.validators and validate_graph:
            plans = dict(typed_storage.collection_values(self.connection, manifest.collection_id))
            plans[key] = values
            write = planning.HierarchyWrite(self.root, key, before)
            for name in declared.validators:
                types.named_validator(name).validate(manifest, plans, write=write)
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
        self._preflight_views([path])
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
        metadata = self.connection.execute(
            "SELECT m.governance_json FROM collection_manifests m JOIN collections c "
            "ON c.collection_id=m.collection_id AND c.manifest_version=m.manifest_version WHERE c.collection_id=?",
            (manifest.collection_id,),
        ).fetchone()[0]
        before = json.loads(self._item(manifest.collection_id, key)["values_json"]) if candidate["action"] == "update" else None
        held_metadata = governance.held_metadata(manifest.schema, candidate, metadata, before)
        frontmatter = {
            "type": "held-record",
            "collection_id": manifest.collection_id,
            "held_id": reference,
            "attempted_action": candidate["action"],
            "held_at": now,
            "why": why,
            "candidate_sha256": hashlib.sha256(candidate_body.encode()).hexdigest(),
            "exomem_view": views.stamp(self._publication.identity, 1, hashlib.sha256(candidate_body.encode()).hexdigest()),
            "diagnostics": _json(diagnostics),
            **json.loads(held_metadata),
            **({"target_item_key": key} if key else {}),
        }
        text = (
            "---\n"
            + vault.serialize_frontmatter(frontmatter)
            + "\n---\n\n```json\n"
            + candidate_body
            + "\n```\n"
        )
        self._publication.business_started = True
        self._execute(
            "INSERT INTO held_candidates (held_id, collection_id, kind, code, candidate_json, diagnostics_json, view_path, updated_at, held_bytes,governance_json,governance_hash) "
            "VALUES (?, ?, 'write-refusal', ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(held_id) DO UPDATE SET "
            "code = excluded.code, candidate_json = excluded.candidate_json, diagnostics_json = excluded.diagnostics_json, updated_at = excluded.updated_at, held_bytes = excluded.held_bytes, governance_json=excluded.governance_json, governance_hash=excluded.governance_hash",
            (
                reference,
                manifest.collection_id,
                error.code,
                encoded,
                _json(diagnostics),
                path,
                now,
                text.encode(),
                held_metadata,
                hashlib.sha256(text.encode()).hexdigest(),
            ),
        )
        self._pending(path, manifest.collection_id, "held", 1, text, manifest=manifest)
        self.handle.release_cache.touch(manifest.collection_id,
                                       f"exomem://collection-held/{manifest.collection_id}/{reference}")
        self._precommit(manifest)
        result = collections.CollectionError(
            error.code,
            error.reason,
            {
                **error.details,
                "held": {"held_id": reference, "path": path, "diagnostics": diagnostics},
            },
        )
        self._publication.bind(result.details)
        return result

    def _release(self, held):
        if held:
            self._preflight_views([held["view_path"]])
            self._publication.business_started = True
            self._publication.retire(self._publication.captured[held["view_path"]])
            self._execute(
                "DELETE FROM held_candidates WHERE held_id = ?", (held["held_id"],)
            )
            self.handle.release_cache.touch(held["collection_id"],
                                           f"exomem://collection-held/{held['collection_id']}/{held['held_id']}")

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
            resolved = records._resolve_bulk_source(self.root, source, self._operation.allows_file, cache)
            if resolved is None:
                raise collections.CollectionError(
                    "INVALID_RECORD_SOURCE", "source must be a preserved Sources or Evidence page"
                )
            relative, guard = resolved
            validated.append(relative)
            guards.append(guard)
        return validated, guards

    def _require_items_capacity(self, cid):
        """Refuse the row past the items-mode ceiling; count once per mutation."""
        if cid not in self._row_counts:
            self._row_counts[cid] = self.connection.execute(
                "SELECT COUNT(*) FROM (SELECT 1 FROM items WHERE collection_id=? LIMIT ?)",
                (cid, ITEMS_MODE_MAX_ROWS)).fetchone()[0]
        if self._row_counts[cid] >= ITEMS_MODE_MAX_ROWS:
            raise collections.CollectionError(
                "COLLECTION_ITEM_LIMIT",
                "the collection is at its items-mode row ceiling; larger data belongs in a NEW summary collection",
                {"items": self._row_counts[cid], "maximum": ITEMS_MODE_MAX_ROWS},
            )
        self._row_counts[cid] += 1

    def _pending_summary(self, manifest, generation):
        """Commit the summary pages' pending basis with the rows; reconcile publishes them.

        The receipt says so: until reconcile, the published page shows an older basis.
        """
        for path in summary.page_paths(manifest):
            self._publication.pending = True
            self._confirm_installed(path)
            self._execute(
                "INSERT INTO projection_state(path,collection_id,row_id,kind,pending_row_version,pending_sha256,state) "
                "VALUES (?,?,NULL,'summary',?,NULL,'pending') ON CONFLICT(path) DO UPDATE SET "
                "pending_row_version=excluded.pending_row_version,pending_sha256=NULL,state='pending'",
                (path, manifest.collection_id, generation),
            )

    def _confirm_installed(self, path):
        """Record a page that a publication installed and no reconcile confirmed yet, before a new basis replaces it.

        Reconcile confirms an installed page on its next pass. A commit in between would
        otherwise clear the page's digest, so its bytes would match neither recorded digest
        and read as a foreign edit that never publishes again.
        """
        row = self.connection.execute(
            "SELECT pending_sha256 FROM projection_state WHERE path=? AND pending_sha256 IS NOT NULL", (path,)
        ).fetchone()
        if row is None:
            return
        batch = self._publication
        try:
            opened = batch._fs().file(batch._parent(path), Path(path).name)
            if not opened.ok:
                return
            with opened.require() as file:
                raw = batch._fs().read(file).require()
        except (held_fs.HeldFsError, OSError):
            return
        if views._digest(raw) == row[0]:
            self._execute("UPDATE projection_state SET published_row_version=pending_row_version,"
                          "published_sha256=pending_sha256 WHERE path=?", (path,))

    def _write_item(self, manifest, key, values, body, path, txn, before, resumed, sources,
                    *, ordinal=0, queue_log=True):
        if before is None and manifest.view_mode == "items":
            self._require_items_capacity(manifest.collection_id)
        if _renders_rows(manifest):
            self._publication.capture_previous(path)
        natural_key = _natural_key(manifest, values)
        payload = tokens.payload_hash(manifest.schema.version, key, values, body)
        version = 1 if before is None else before["row_version"] + 1
        manifest_metadata = self.connection.execute(
            "SELECT m.governance_json FROM collection_manifests m JOIN collections c "
            "ON c.collection_id=m.collection_id AND c.manifest_version=m.manifest_version WHERE c.collection_id=?",
            (manifest.collection_id,),
        ).fetchone()[0]
        metadata = governance.row_metadata(manifest.schema, values, manifest_metadata)
        # The collection is the one encoding authority; json-v1 keeps canonical
        # JSON, typed-v1 keeps ordinal typed columns and no JSON copy.
        encoding = typed_storage.collection_encoding(self.connection, manifest.collection_id)
        stored = _json(values) if encoding == typed_storage.JSON_V1 else None
        changed = {"natural_key": natural_key, "row_version": version, "values_json": stored,
                   "body": body, "payload_hash": payload, "updated_txn": txn["txn_id"],
                   "governance_json": metadata}
        if before is None:
            cursor = self._execute(tables.INSERT_ITEM, {
                **changed, "collection_id": manifest.collection_id, "item_key": key,
                "schema_version": manifest.schema.version, "view_path": path,
                "created_txn": txn["txn_id"], "encoding": encoding,
            })
            row_id = cursor.lastrowid
        else:
            row_id = before["row_id"]
            self._execute(tables.UPDATE_ITEM, {**changed, "target_row_id": row_id})
        if encoding == typed_storage.JSON_V1:
            # The JSON payload mints its json-v1 version_identity in the same statement.
            self._execute(tables.INSERT_VERSION, {
                "row_id": row_id, "row_version": version, "values_json": stored, "body": body,
                "payload_hash": payload, "txn_id": txn["txn_id"],
            })
        else:
            typed_storage.write_version(
                index_migrations.AccountedWriter(self.connection, self._execute),
                typed_storage.require_layout(self.connection, manifest.collection_id),
                row_id=row_id, row_version=version, values=values, body=body, payload_hash=payload,
                txn_id=txn["txn_id"], schema_version=manifest.schema.version,
            )
        index_migrations.maintain_item(index_migrations.AccountedWriter(self.connection, self._execute),
                                       manifest.collection_id, row_id, key, version, values,
                                       previous=json.loads(before["values_json"]) if before else None)
        for source_ordinal, source in enumerate(sources):
            self._execute(tables.INSERT_SOURCE, {"row_id": row_id, "row_version": version,
                          "ordinal": source_ordinal, "source_ref": source})
        self._execute(tables.INSERT_EFFECT, {
            "txn_id": txn["txn_id"], "ordinal": ordinal, "row_id": row_id, "item_key": key,
            "effect": "resume" if resumed else "insert" if before is None else "update",
            "effect_label": "replan" if manifest.semantic_profile == "planning" else "correction" if before else None,
            "version_before": None if before is None else before["row_version"], "version_after": version,
            "hash_before": None if before is None else before["payload_hash"], "hash_after": payload,
        })
        if _renders_rows(manifest):
            _, text = self._render_view({"kind": "item", "row_id": row_id}, manifest)
            self._pending(path, manifest.collection_id, "item", version, text, row_id, manifest=manifest)
        elif queue_log and manifest.view_mode == summary.SUMMARY:
            self._pending_summary(manifest, txn["generation_after"])
        elif queue_log:
            self._pending_log(manifest, txn)
        self._release(resumed)
        self.handle.release_cache.touch(manifest.collection_id,
                                       f"exomem://{types.type_for_manifest(manifest).item_type}/{manifest.collection_id}/{key}")
        return tokens.item_version(manifest.collection_id, key, version, payload)

    def _pending_log(self, manifest, txn):
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
        order = "DESC" if manifest.storage.descriptor.get("insertion") == "newest-first" else "ASC"
        cursor = self.connection.execute(
            f"SELECT item_key, values_json, row_version, payload_hash, row_id, collection_id, encoding FROM items WHERE collection_id = ? ORDER BY created_txn {order}, row_id {order}",
            (manifest.collection_id,),
        )
        names = [column[0] for column in cursor.description]
        blocks = typed_storage.hydrate(self.connection, [dict(zip(names, row, strict=True)) for row in cursor])
        identity = self._publication.identity
        text = frame + "".join(
            record_formats.render_markdown_log_item(
                manifest, json.loads(block["values_json"]), block["item_key"], "\n",
                view_stamp=views.stamp(identity, block["row_version"], block["payload_hash"]),
            )
            for block in blocks
        )
        self._pending(manifest.storage.source, manifest.collection_id, "log", txn["generation_after"], text)

    def _fresh_authorization(self):
        """A new mutation-authority snapshot, independent of any open operation."""
        return governance.OperationAuthorization(
            self.root, self.connection, mutation=True, cache=self.handle.release_cache
        )

    def bulk_upsert_records(self, collection, *, rows, why, expected_container_hash,
                            source=None, on_reject="abort", request_id=None, _import=None):
        """Plan Records rows under one guard; commit one transition with N effects.

        ``_import`` is an import job's batch settlement (``importer``): its
        ``source`` is the job's proved source and it is called with the result
        inside this transaction, so the job's checkpoint commits or rolls back
        with the rows.
        """
        records._validate_why(why)
        if on_reject not in {"abort", "skip"} or not isinstance(rows, list | tuple):
            raise collections.CollectionError("INVALID_BULK_ROWS", "rows must be a list and on_reject abort or skip")
        if not rows:
            raise collections.CollectionError("BULK_UPSERT_EMPTY", "rows must not be empty")
        if len(rows) > BULK_UPSERT_MAX_ROWS:
            raise collections.CollectionError(
                "BULK_UPSERT_TOO_MANY_ROWS",
                f"a bulk upsert takes at most {BULK_UPSERT_MAX_ROWS} rows per call; split the import "
                "into batches chained by after_container_hash",
                {"rows": len(rows), "maximum": BULK_UPSERT_MAX_ROWS,
                 "batches_needed": -(-len(rows) // BULK_UPSERT_MAX_ROWS),
                 "chain_with": "after_container_hash"},
            )
        if source is not None and type(source) is not str:
            raise collections.CollectionError("INVALID_BULK_ROWS", "source must be a string")
        arguments = dict(rows=rows, why=why, expected_container_hash=expected_container_hash,
                         source=source, on_reject=on_reject)
        with self._mutation():
            identity, digest, replay = self._identity("bulk_upsert", collection, arguments, request_id)
            if replay is not None:
                return replay
            row, manifest, _ = self._collection(collection, facade_profile="records")
            current_hash = self._container(row)
            records._expect_hash(expected_container_hash, current_hash, "container")
            batch_id = uuid.uuid4().hex[:12]
            has_sources = records._bulk_sources_field(manifest)
            is_log = manifest.storage.strategy == "markdown-log"
            cache, source_guards, seen = {}, {}, {}
            outcomes, plans = [], []
            # Only a filename recipe consults occupied keys; fetching them streams every row subject.
            occupied = (ChainMap({}, self.handle.release_cache.state(manifest.collection_id).occupied_path_keys)
                        if manifest.item_filename else {})
            for index, raw in enumerate(rows):
                if (not isinstance(raw, Mapping) or set(raw) - records._BULK_ROW_KEYS
                        or not isinstance(raw.get("item"), Mapping)):
                    outcomes.append(records._bulk_reject(index, "INVALID_BULK_ROW",
                                    "a row is an object of item, optional body and optional source"))
                    continue
                body = raw.get("body")
                reference = raw["source"] if raw.get("source") is not None else source
                try:
                    records._refuse_excluded_authored_names(raw["item"])
                    _refuse_view_stamp(manifest, raw["item"])
                    if body is not None:
                        records._validate_body(body)
                        if body and is_log:
                            raise collections.CollectionError("UNREPRESENTABLE_RECORD_BODY",
                                                              "markdown-log storage cannot represent item bodies")
                    if reference is None:
                        outcomes.append(records._bulk_reject(index, "SOURCE_REQUIRED",
                                                            "a row needs a preserved source reference"))
                        continue
                    resolved = (
                        _import.source
                        if _import is not None
                        else records._resolve_bulk_source(
                            self.root, reference, self._operation.allows_file, cache
                        )
                    )
                    if resolved is None:
                        outcomes.append(records._bulk_reject(index, "SOURCE_NOT_FOUND",
                                        "source is not a preserved Sources or Evidence page", source=reference))
                        continue
                    source_rel, guard = resolved
                    values = records._bulk_row_values(manifest, raw["item"], source_rel)
                except collections.CollectionError as error:
                    outcomes.append(records._bulk_error_row(index, error))
                    continue
                derived = collections.derived_item_key(manifest, values)
                key = records._validate_item_key(derived or str(uuid.uuid4()))
                base = dict(item_key=key, identity="natural-key" if derived else "generated", source=source_rel)
                if derived is not None and key in seen:
                    outcomes.append(records._bulk_reject(index, "DUPLICATE_ROW_KEY",
                                    "an earlier row in this request already holds this natural key",
                                    duplicate_of=seen[key], **base))
                    continue
                try:
                    self._twins(manifest.collection_id, key, _natural_key(manifest, values))
                except collections.CollectionError as error:
                    outcomes.append(records._bulk_reject(index, error.code, error.reason, **error.details, **base))
                    continue
                before = self._item(manifest.collection_id, key)
                effective_body = body if body is not None else before["body"] if before else ""
                if before and before["payload_hash"] == tokens.payload_hash(manifest.schema.version, key, values, effective_body):
                    if derived is not None:
                        seen[key] = index
                    outcomes.append({"index": index, **base, "outcome": "unchanged"})
                    continue
                rationale = f"{why} | bulk {batch_id} {index + 1}/{len(rows)}" + (
                    "" if has_sources else f" src {source_rel}"
                )
                if len(rationale.encode()) > records._MAX_WHY_BYTES or "\n" in rationale:
                    outcomes.append(records._bulk_reject(index, "AUDIT_RATIONALE_TOO_LONG",
                                    "the audit rationale for this row exceeds its bound", **base))
                    continue
                if derived is not None:
                    seen[key] = index
                if before:
                    path = before["view_path"]
                elif is_log:
                    path = f"{manifest.storage.source}#{key}"
                elif manifest.view_mode == summary.SUMMARY:
                    path = None
                else:
                    path = collections.render_item_path(manifest, values, key, occupied_path_keys=occupied) if manifest.item_filename else f"{manifest.storage.source}/{key}.md"
                if path is not None:
                    occupied[collections._portable_path_key(path)] = 1
                source_guards[source_rel] = guard
                plans.append((key, values, effective_body, path, before, source_rel))
                outcomes.append({"index": index, **base, "outcome": "updated" if before else "inserted"})
            counts = {name: sum(outcome["outcome"] == name for outcome in outcomes)
                      for name in ("inserted", "updated", "unchanged", "rejected")}
            result = dict(operation="bulk_upsert", collection_id=manifest.collection_id,
                          batch_id=batch_id, committed=False, on_reject=on_reject, rows=outcomes,
                          counts=counts, before_container_hash=current_hash, after_container_hash=current_hash)
            if not plans or (counts["rejected"] and on_reject == "abort"):
                if _import is not None:
                    _import(self, result)
                return result
            if _renders_rows(manifest):
                self._preflight_views(path for _, _, _, path, _, _ in plans)
            txn = self._txn(row, manifest, "bulk_upsert", why, identity, digest,
                            effects=[{"key": key, "payload_hash": tokens.payload_hash(manifest.schema.version, key, values, body),
                                      "sources": [source]} for key, values, body, _, _, source in plans])
            for ordinal, (key, values, body, path, before, source_rel) in enumerate(plans):
                self._write_item(manifest, key, values, body, path, txn, before, None, [source_rel],
                                 ordinal=ordinal, queue_log=False)
            if is_log:
                self._pending_log(manifest, txn)
            elif manifest.view_mode == summary.SUMMARY:
                self._pending_summary(manifest, txn["generation_after"])
            result.update(committed=True, after_container_hash=self._advance(txn),
                          first_transition=txn["transition_id"], last_transition=txn["transition_id"])
            for outcome in outcomes:
                if outcome["outcome"] in {"inserted", "updated"}:
                    outcome["transition_id"] = txn["transition_id"]
            self._precommit(manifest)
            for guard in source_guards.values():
                self._recheck_guard(guard, publication=True)
            self._insert_txn(txn, result)
            if _import is not None:
                _import(self, result)
        writer_lease.mark_active_mutation_committed()
        advisory = None
        for key, values, _, path, before, _ in plans:
            advisory = records._due_state_carrier(self.root, manifest, path=path or manifest.path, key=key,
                                                  values=values, previous=json.loads(before["values_json"]) if before else None)
        return {"due_state": advisory, **result} if advisory else result

    def _item_carriers(self, result, manifest, *, path, key, values, previous=None):
        """Refresh derived state only after a new item write has committed."""
        if result["outcome"] != "committed":
            return result
        writer_lease.mark_active_mutation_committed()
        advisory = records._due_state_carrier(
            self.root, manifest, path=path, key=key, values=values, previous=previous,
        )
        sweep = records._capture_sweep_carrier(self.root, manifest, key=key)
        result = {"due_state": advisory, **result} if advisory else result
        return {"capture_sweep": sweep, **result} if sweep else result

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
        with self._mutation():
            identity, digest, replay = self._identity("append", collection, arguments, request_id)
            if replay is not None:
                return replay
            row, manifest, declared = self._collection(collection)
            resumed = self._held(manifest.collection_id, held)
            if (item is None and resumed is None) or (
                item is not None and not isinstance(item, Mapping)
            ):
                raise collections.CollectionError("INVALID_ITEM", "item must be an object")
            values = (
                records._apply_held_overrides(
                    resumed["candidate"].get("item", {}), {} if item is None else item
                )
                if resumed
                else item
            )
            if resumed and resumed["candidate"]["action"] != "append":
                raise records._held_not_found()
            if body is None and resumed:
                body = resumed["candidate"].get("body")
            if body is None:
                body = ""
            records._validate_body(body)
            records._refuse_excluded_authored_names(values)
            if manifest.storage.strategy == "markdown-log" and body:
                raise collections.CollectionError(
                    "UNREPRESENTABLE_RECORD_BODY",
                    "markdown-log storage cannot represent item bodies",
                )
            if declared.kind == "intended":
                values = planning.normalize_item(
                    values,
                    vault_root=self.root,
                    validate_motivation=planning.motivation_is_governed(manifest),
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
                        affected_paths=_affected(manifest, existing["view_path"]),
                        payload_hash=payload,
                        outcome="replayed",
                        audit_correlation=insert[0],
                    )
                    self._precommit(manifest)
                    if delivery_guard:
                        self._recheck_guard(delivery_guard)
                    self._release(resumed)
                    self._publication.bind(result)
                else:
                    if manifest.storage.strategy == "markdown-log":
                        path = f"{manifest.storage.source}#{key}"
                    elif manifest.view_mode == summary.SUMMARY:
                        path = None
                    elif manifest.item_filename:
                        path = collections.render_item_path(
                            manifest, values, key,
                            occupied_path_keys=self.handle.release_cache.state(manifest.collection_id).occupied_path_keys,
                        )
                    else:
                        path = f"{manifest.storage.source}/{key}.md"
                    verified, source_guards = self._sources(sources)
                    self._preflight_views(([path] if _renders_rows(manifest) else [])
                                          + ([resumed["view_path"]] if resumed else []))
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
                        affected_paths=_affected(manifest, path),
                        payload_hash=payload,
                        outcome="committed",
                        audit_correlation=txn["transition_id"],
                    )
                    self._precommit(manifest)
                    if delivery_guard:
                        self._recheck_guard(delivery_guard)
                    for guard in source_guards:
                        self._recheck_guard(guard)
                    self._insert_txn(txn, result)
        if isinstance(result, collections.CollectionError):
            raise result
        return self._item_carriers(result, manifest, key=key, values=values,
                                   path=result["affected_paths"][0] if manifest.view_mode == "items" else manifest.path)

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
        with self._mutation():
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
                if changes is not None and not isinstance(changes, Mapping):
                    raise collections.CollectionError(
                        "INVALID_RECORD_CHANGES", "changes must be a non-empty object"
                    )
                changes = records._apply_held_overrides(
                    candidate.get("changes", {}), {} if changes is None else changes
                )
                delete_fields = delete_fields or tuple(candidate.get("delete_fields", ()))
                body = candidate.get("body") if body is None else body
            if not isinstance(changes, Mapping) or (
                not changes and not delete_fields and body is None and not refresh_presentation
            ):
                raise collections.CollectionError(
                    "INVALID_RECORD_CHANGES", "changes must be a non-empty object"
                )
            if refresh_presentation and (
                manifest.record_presentation is None and manifest.item_presentation is None
            ):
                raise collections.CollectionError(
                    "INVALID_RECORD_PRESENTATION", "collection has no presentation recipe"
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
                if refresh_presentation and not changes and not delete_fields and body is None:
                    projection = self.connection.execute(
                        "SELECT COALESCE(pending_sha256, published_sha256) FROM projection_state WHERE path = ?",
                        (before["view_path"],),
                    ).fetchone()
                    rendered = record_formats.render_markdown_item(
                        manifest, final, key, final_body,
                        view_stamp=views.stamp(self._publication.identity, before["row_version"], before["payload_hash"]),
                    )
                    if projection and projection[0] == hashlib.sha256(rendered.encode()).hexdigest():
                        raise collections.CollectionError(
                            "NOOP_RECORD_PRESENTATION", "managed presentation is already current"
                        )
                self._twins(manifest.collection_id, key, _natural_key(manifest, final))
                verified, source_guards = self._sources(sources)
                self._preflight_views(([before["view_path"]] if _renders_rows(manifest) else [])
                                      + ([resumed["view_path"]] if resumed else []))
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
                    affected_paths=_affected(manifest, before["view_path"]),
                    payload_hash=None,
                    outcome="committed",
                    audit_correlation=txn["transition_id"],
                )
                self._precommit(manifest)
                for guard in source_guards:
                    self._recheck_guard(guard)
                self._insert_txn(txn, result)
        if isinstance(result, collections.CollectionError):
            raise result
        return self._item_carriers(result, manifest, path=before["view_path"] or manifest.path,
                                   key=key, values=final, previous=old_values)

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
        with self._mutation():
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
            # A populated collection never changes view mode in place; refuse
            # before any manifest, mapping, file, guard or cursor work.
            mode_change = proposed.view_mode != current.view_mode
            if mode_change and summary.populated(self.connection, current.collection_id):
                raise summary.mode_change_refused(current.view_mode, proposed.view_mode)
            records._refuse_excluded_manifest_fields(proposed)
            if proposed.view_diagnostics:
                diagnostic = proposed.view_diagnostics[0]
                raise collections.CollectionError(diagnostic.code, diagnostic.reason)
            record_formats.validate_storage_contract(proposed)
            if declared.kind == "intended":
                planning.require_planning_profile(proposed)
            plans = {
                key: self._validate(proposed, declared, key, values, operation="revise", validate_graph=False)
                for key, values in typed_storage.collection_values(self.connection, current.collection_id)
            }
            for name in declared.validators:
                types.named_validator(name).validate(proposed, plans, write=None)
            retired_pages = summary.page_paths(current) if mode_change else ()
            self._preflight_views([current.path, *retired_pages, *(path for (path,) in self.connection.execute(
                "SELECT view_path FROM items WHERE collection_id=? AND view_path IS NOT NULL UNION ALL "
                "SELECT view_path FROM held_candidates WHERE collection_id=?",
                (current.collection_id, current.collection_id),
            ))])
            if mode_change and (self._publication.pending
                                or summary.populated(self.connection, current.collection_id)
                                or summary.publication_outstanding(self.connection, current.collection_id)):
                # Preflight can hold an offline edit; an unclassified edit or unpublished view also keeps the mode.
                raise summary.mode_change_refused(current.view_mode, proposed.view_mode)
            txn = self._txn(row, proposed, "revise", why, identity, digest)
            self._manifest(proposed, manifest_text, txn["manifest_version_after"], txn["txn_id"])
            if mode_change:
                self._execute("UPDATE collections SET view_mode=? WHERE collection_id=?",
                              (proposed.view_mode, current.collection_id))
                if proposed.view_mode == summary.SUMMARY and typed_storage.migrate_batch(
                        index_migrations.AccountedWriter(self.connection, self._execute),
                        current.collection_id, tuple(proposed.schema.fields), limit=1) != "ready":
                    raise RuntimeError("an empty collection publishes its typed layout at once")
                for path in retired_pages:
                    self._publication.retire(self._publication.captured[path])
            self._pending_summary(proposed, txn["generation_after"])
            self._execute(
                "UPDATE collections SET manifest_version = ?, audit_reader_version = 2 WHERE collection_id = ?",
                (txn["manifest_version_after"], current.collection_id),
            )
            metadata = governance.manifest_metadata(manifest_text)
            self._execute("UPDATE items SET governance_json=? WHERE collection_id=? AND item_key=?",
                ((governance.row_metadata(proposed.schema, values, metadata), current.collection_id, key)
                 for key, values in plans.items()), many=True)
            for held_id, kind, encoded, captured, path in self.connection.execute(
                "SELECT held_id,kind,candidate_json,governance_json,view_path FROM held_candidates WHERE collection_id=?",
                (current.collection_id,),
            ).fetchall():
                if kind == "view-correction":
                    preserved = governance._metadata(captured)
                    preserved["projects"] = governance._metadata(metadata)["projects"]
                    held_metadata = _json(preserved)
                else:
                    candidate = records._decode_held_value(json.loads(encoded))
                    before = plans.get(candidate.get("item_key"))
                    held_metadata = governance.held_metadata(proposed.schema, candidate, metadata, before)
                self._execute("UPDATE held_candidates SET governance_json=? WHERE held_id=?",
                    (held_metadata, held_id))
                if kind == "view-correction" and held_metadata != captured:
                    version, text = self._render_view({"kind": "held", "path": path, "held_id": held_id}, proposed)
                    self._pending(path, current.collection_id, "held", version, text, manifest=proposed)
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
            self._precommit(proposed)
            self._insert_txn(txn, receipt)
        writer_lease.mark_active_mutation_committed()
        return receipt

    def discard_held(self, collection, *, held, why):
        records._validate_why(why)
        with self._mutation():
            _, manifest, _ = self._collection(collection)
            candidate = self._held(manifest.collection_id, held)
            if candidate is None:
                raise records._held_not_found()
            self._precommit(manifest)
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
            self._publication.bind(receipt)
        writer_lease.mark_active_mutation_committed()
        return receipt
