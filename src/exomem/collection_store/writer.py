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
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

from sqlalchemy import bindparam, select

from .. import (
    held_fs,
    memory_refs,
    mutation_terminal,
    planning,
    record_formats,
    records,
    state_paths,
    sync_providers,
    vault,
    writer_lease,
)
from .. import structured_collections as collections
from ..governance import connector_boundary
from ..governance.principal import effective_principal
from ..planning_values import PlanningValues
from ..query_engine.indexes import IndexDeclarationError
from . import (
    chain,
    connection,
    derived_rows,
    edit_back,
    governance,
    index_migrations,
    log_view,
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
        # The settled view edit the current leaf call applies (edit_back), else None.
        self._view_edit = None

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

    def _publication_refused(self, cid):
        """Whether the write authorization refuses this collection: the check ``_precommit`` ends a publication on."""
        try:
            self._operation.require_collection(cid, refresh=True)
        except collections.CollectionError as error:
            if error.code != "COLLECTION_NOT_FOUND":
                raise
            return True
        return False

    def _recheck_guard(self, guard, *, publication=False):
        self._publication.guards[id(guard)] = (guard, publication)
        try:
            if isinstance(guard, views._AliasInput):
                with self._publication._boundary():
                    guard.recheck(self.root)
            else:
                guard.recheck(self.root)
        except vault.PathGuardError as error:
            if publication:
                raise records._publication_error(error) from error
            raise

    @contextmanager
    def _mutation(self, *, reconcile=False, stop=None):
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
                batch = self._publication = views.PublicationBatch(self, stop=stop)
                self._row_counts = {}
                if self._view_edit is not None and not self._view_edit.get("partial_log"):
                    # The edited bytes become canonical here, so displacing them loses nothing.
                    batch.accept(self._view_edit["path"], self._view_edit["digest"])
                try:
                    yield
                    if self._view_edit is not None and not self._view_edit.get("partial_log") and batch.business_started:
                        # A later valid edit of a view resolves the holds of its earlier bytes there.
                        self._resolve_in_place(self._view_edit["path"], self._view_edit["base"])
                except _RecoveryOnly:
                    recovery_only = True
                if not reconcile:
                    # Exact item and manifest intents are durable; the projector installs them after save.
                    # The projection_state.kind CHECK fixes the ordinary kinds that defer publication.
                    batch.publications = {path: publication for path, publication in batch.publications.items()
                                          if publication.projection is None
                                          or publication.projection["kind"] not in ("item", "manifest")}
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

    @staticmethod
    def _rows_writable(manifest, action):
        """The one gate on row mutations: a derived collection's rows change only through its imports."""
        if manifest.derived:
            raise derived_rows.refused(action)

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
        # Canonical reconstruction cannot re-enter the release check that needs this manifest.
        manifest = collections.parse_manifest_bytes(
            self.root, row["manifest_path"], text.encode(),
            planning_values=PlanningValues(self.root, server_side=True),
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
    def read_snapshot(self, *, mutation=False):
        """Join canonical reads under one SQLite and policy snapshot.

        ``mutation`` reads under the policy a write is held to: a collection withheld in part
        reads as absent, as the write itself would find it.
        """
        self._require_operation_context()
        self.handle.release_cache.check()
        if self.connection.in_transaction:
            with self._authorization(mutation=mutation):
                yield
            return
        self.connection.execute("BEGIN")
        try:
            with self._authorization(mutation=mutation):
                yield
        finally:
            self.connection.execute("ROLLBACK")

    @contextmanager
    def read_collection(self, selector, *, facade_profile: str | None = None, mutation=False):
        """Resolve and read one collection under one SQLite and policy snapshot."""
        if facade_profile is None and isinstance(selector, collections.CollectionManifest):
            facade_profile = selector.semantic_profile
        with self.read_snapshot(mutation=mutation):
            manifest = self._collection(selector, facade_profile=facade_profile)[1]
            yield self._operation.field_plan(manifest).manifest

    def discover_collections(self, *, authorize_path=None, max_candidates=512, max_raw_candidates=512):
        from . import ownership
        from .preview import production_bound

        with self.read_snapshot():
            proof = ownership.current(self.root, conn=self.connection, private=not production_bound(self.root))
            entries = [(entry["collection_id"], entry["manifest_path"]) for entry in proof.entries
                       if entry["collection_id"] in proof.admitted]
            manifests = []
            for cid, path in sorted(entries, key=lambda entry: entry[1]):
                if authorize_path is not None and not authorize_path(path):
                    continue
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

    def admitted_item_metadata(self, cid, key, path):
        """Admit one complete current item from canonical metadata, without reading its payload."""
        from .preview import selected_writer

        selected = selected_writer(self.root, cid)
        if selected is None or selected.handle._inspection_identity is not self.handle._inspection_identity:
            return None
        with self.read_snapshot():
            self._operation.require_complete_fields(cid)
            row = self.connection.execute(
                "SELECT view_path FROM items WHERE collection_id=? AND item_key=?", (cid, key)
            ).fetchone()
            if row is None or row[0] != path:
                return None
            manifest_path = self.connection.execute(
                "SELECT manifest_path FROM collections WHERE collection_id=?", (cid,)
            ).fetchone()[0]
            manifest = next(governance.iter_subjects(
                self.connection, cid, self._operation.logical_vault_id,
                identity=manifest_path, include_held=False), None)
            item = next(governance.iter_subjects(
                self.connection, cid, self._operation.logical_vault_id, row_key=key, include_held=False), None)
            if manifest is None or item is None or any(
                    self._operation.decision(subject).level < 6 for subject in (manifest, item)):
                return None
            return item

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
        from . import history

        history.verify(self.connection, manifest.collection_id, proofs=self.handle.release_cache.history)
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
        # Global guards and audit state cannot depend on whether hidden rows
        # happen to exist. Limited callers receive the admitted snapshot only.
        unrestricted = connector_boundary.unrestricted(self.root, self._operation.who)
        if release is not None:
            complete, committed = unrestricted and release.complete, release.released
        else:
            complete = unrestricted and len(allowed_rows) == sum(
                isinstance(subject.row_id, int) for subject in catalog
            )
            committed = len(allowed_rows)
        held_paths = {subject.basis.subject.path for subject in catalog if subject.row_id in held_ids}
        pending = 0
        pending_history = {}
        for path, row_id, kind, published in self.connection.execute(
            "SELECT path,row_id,kind,published_sha256 FROM projection_state WHERE collection_id=? AND state='pending'",
            (manifest.collection_id,),
        ):
            if kind == "history":
                # A hidden-only write may queue a page without changing any released output.
                if self._operation.projection_decision(path).level >= 6:
                    pending_history[path] = published
            else:
                pending += (kind == "manifest" or row_id in allowed_rows or (kind in ("log", "summary") and complete)
                            or (kind == "held" and path in held_paths))
        if pending_history:
            from . import history

            context = history.PageContext(self.connection, self.root, manifest, cache=self.handle.release_cache)
            published_history = {path: digest for path, digest in pending_history.items() if digest is not None}
            # Every render has a digest, so an admitted unpublished page is already pending.
            pending += len(pending_history) - len(published_history)
            pending += sum(digest != published_history[path] for path, digest in context.digests(published_history))
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
        if complete:
            # A migration reason can identify an unreleased source row, so only a whole-owner view includes it.
            migration = typed_storage.migration_diagnostic(self.connection, manifest.collection_id)
            if migration is not None:
                diagnostics.append({"code": "NATIVE_MIGRATION_PENDING", "reason": "the original encoding remains active",
                                    "migration": migration})
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
        if manifest.derived:
            payload["contract"]["derived"] = True
            payload["derived_rows"] = derived_rows.rebuild_status(self.connection, self.handle.path,
                                                                  manifest.collection_id)
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
        from . import history

        return history.audit(self, row, complete=complete)

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
            stored = self.request_receipt(identity)
            if stored is not None:
                if stored["request_hash"] != digest:
                    raise collections.CollectionError(
                        "IDEMPOTENCY_KEY_REUSED",
                        "request identity already names different arguments",
                    )
                return identity, digest, mutation_terminal._CanonicalRequestReplay(stored["receipt"])
        return identity, digest, None

    def request_receipt(self, request_id: str) -> dict[str, Any] | None:
        """The transaction a request id committed: its request hash, collection, receipt and commit time.

        None when no transaction carries the id. A collection this caller may not see reads as absent,
        so the refusal is the one a missing collection gives. Call it inside a snapshot or a mutation.
        """
        found = self.connection.execute(
            "SELECT request_hash, collection_id, receipt_json, committed_at FROM txns WHERE request_id = ?",
            (request_id,),
        ).fetchone()
        if found is None:
            return None
        self._operation.require_collection(found[1])
        return {
            "request_hash": found[0],
            "collection_id": found[1],
            "receipt": json.loads(found[2]),
            "committed_at": found[3],
        }

    def published_views(self, collection_id: str) -> list[tuple[str, str, str | None, str]]:
        """The store's view of each generated file of a collection: path, kind, published digest, state.

        A state other than ``current`` is a file the store has not yet published or has held.
        Call it inside a snapshot.
        """
        return [
            tuple(row)
            for row in self.connection.execute(
                "SELECT path, kind, published_sha256, state FROM projection_state WHERE collection_id = ?",
                (collection_id,),
            )
        ]

    def current_guards(self, selector, item_key: str | None = None) -> tuple[str, str | None]:
        """The container hash a write to this collection must carry, and the item's version when one is named.

        Raises ``RECORD_NOT_FOUND`` for a named item that does not exist.
        """
        with self.read_snapshot():
            row = self._collection(selector)[0]
            if item_key is None:
                return self._container(row), None
            item = self._item(row["collection_id"], item_key)
            if item is None:
                raise collections.CollectionError("RECORD_NOT_FOUND", "record key does not exist")
            return self._container(row), self._version(item)

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
        view_edit = (self._view_edit is not None or operation in {"view_move", "view_frame_edit"}) and not control
        recorded = edit_back.OPERATION if self._view_edit is not None and not control else operation
        event = {
            "transition_id": transition,
            "collection_id": manifest.collection_id,
            "operation": recorded,
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
            "operation": recorded,
            "profile_operation": f"plan_{'add' if operation == 'append' else 'update' if operation == 'view_move' else operation}"
            if manifest.semantic_profile == "planning" and not control and operation != "view_frame_edit"
            else None,
            "generation_before": generation,
            "generation_after": generation + (not control),
            "manifest_version_before": before_manifest,
            "manifest_version_after": after_manifest,
            "actor": edit_back.ACTOR if view_edit else effective_principal().audience_id,
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
        warnable = mutation_terminal.valid_collection_receipt({
            **receipt, "warnings": [*receipt.get("warnings", []), "projection_pending"],
        })
        if txn["operation"] == "view_frame_edit":
            # This private leaf has no item effect and cannot truthfully return a Records update receipt.
            valid = (set(receipt) == {"operation", "collection_id", "transition_id", "commit_seq",
                                      "before_container_hash", "after_container_hash"}
                     and all(receipt[key] == txn[key] for key in
                             ("operation", "collection_id", "transition_id", "commit_seq"))
                     and receipt["before_container_hash"] == tokens.container_hash(
                         txn["collection_id"], txn["generation_before"], txn["prev_event_hash"])
                     and receipt["after_container_hash"] == tokens.container_hash(
                         txn["collection_id"], txn["generation_after"], txn["event_hash"])
                     and txn["generation_after"] == txn["generation_before"] + 1
                     and txn["profile_operation"] is None)
        elif txn["operation"] in mutation_terminal.CONTROL_OPERATIONS:
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
            warnable = valid  # The bulk receipt's existing envelope supports publication warnings.
        if not valid:
            raise RuntimeError("writer constructed an invalid collection receipt")
        data = {**txn, "receipt_json": _json(receipt)}
        from . import history

        through = history.verify(self.connection, txn["collection_id"],
                                 proofs=self.handle.release_cache.history, pending=data)
        self._execute("UPDATE collections SET verified_through_txn=? WHERE collection_id=?",
                      (through, txn["collection_id"]))
        manifest = self._collection_manifest(self._collection_row(txn["collection_id"]))[0]
        self._pending_basis(history.main_path(manifest), txn["collection_id"], "history", 0)
        self._execute(tables.INSERT_TXN, data)
        if self._publication.deferred_create is None and warnable:
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
        if kind in {"manifest", "item", "held", "summary", "log", "history"}:
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
        return views.render_view(self.connection, self._publication.identity, projection, manifest, root=self.root,
                                 cache=self.handle.release_cache, operation=self._operation,
                                 observe_read=self._recheck_guard)

    def _register_view_input(self, projection, raw, token, slot, *, source=None):
        """Classify bytes found at a view path (``slot`` "offline") or displaced by an install.

        True: the bytes are canonical or preserved in a hold, so an install may displace
        them. False: they wait for edit-back or for their governance context, in place.
        Without the edit-back release, a valid or formatting-only edit waits in place, as
        before edit-back existed.
        """
        if projection["kind"] == "history":
            # History is generated read-only output; edited bytes carry no canonical meaning.
            self._publication.accept(projection["path"], hashlib.sha256(raw).hexdigest())
            return True
        if views.unresolved_target(projection, source):
            _, manifest, _ = self._collection(projection["collection_id"])
            _, text = self._render_view(projection, manifest)
            if raw != text.encode():
                item = self._item(projection["collection_id"], self.connection.execute(
                    "SELECT item_key FROM items WHERE row_id=?", (projection["row_id"],)).fetchone()[0])
                self._hold_detached(manifest, projection["path"], raw, edit_back.CONFLICT,
                                    item=item, token=token, slot=slot)
                self._publication.accept(projection["path"], hashlib.sha256(raw).hexdigest())
                return True
        if projection["kind"] == "log":
            return self._register_log_input(projection, raw, token, slot, source=source)
        batch = self._publication
        path = projection["path"]
        digest = hashlib.sha256(raw).hexdigest()
        displaced = slot != "offline"
        observed = None if batch.observed is None else batch.observed.get(path)
        if (not displaced and batch.observed is not None and projection["kind"] in edit_back.EDITABLE_KINDS
                and (observed is None or observed.digest != digest)):
            # Edit-back settled other bytes; these wait for their own settled observation.
            batch.pending = True
            return False
        found = edit_back.classify(self, projection, raw, refusal=observed.refusal if observed else (),
                                   edit_back=batch.edit_back)
        code = found.code
        if code == edit_back.SAME and batch.edit_back:
            batch.accept(path, digest)
            if not displaced:
                self._resolve_in_place(path, found.current)
            return True
        if code is None or code == edit_back.SAME:
            if not displaced or not batch.edit_back:
                # A valid edit at the current base is edit-back's to apply once it settles.
                batch.pending = True
                return False
            code = edit_back.CONFLICT  # A1: an unseen edit met this publication
        if code == edit_back.FOREIGN:
            batch.foreign_discovered = True
        in_place = not displaced and batch.edit_back and edit_back.kept_in_place(code, projection["kind"])
        reference = (edit_back.held_id(path, base=found.base) if in_place
                     else edit_back.held_id(path, raw=raw))
        existing = self.connection.execute(
            "SELECT held_bytes FROM held_candidates WHERE held_id=?", (reference,)
        ).fetchone()
        preserved = existing is not None and existing[0] == raw
        if not preserved and not in_place:
            # Bytes that a hold at the view path already preserves stay held once.
            preserved = self.connection.execute(
                "SELECT 1 FROM held_candidates WHERE collection_id=? AND kind='view-correction' AND held_bytes=? "
                "AND json_extract(diagnostics_json,'$[0].path')=? LIMIT 1",
                (projection["collection_id"], raw, path),
            ).fetchone() is not None
        if not preserved:
            diagnostics = {
                "code": code, "path": path, "token": token, "slot": slot, "sha256": digest,
                "stamp": found.stamp, "base_row_version": found.base, "current_row_version": found.current,
                **({"issues": list(found.issues)} if found.issues else {}),
            }
            if not self._hold_view_correction(projection, code, raw, reference, diagnostics, found.base, source):
                return False
        batch.accept(path, digest)
        if in_place:
            batch.kept.add(path)
        return True

    def _register_log_input(self, projection, raw, token, slot, *, source=None):
        """Classify bytes found at a log view path or displaced by its install, block by block (design §6).

        Without the edit-back release a log edit waits on disk, as an item edit does. Under it,
        once edit-back has applied the edited blocks, each remaining block that needs the owner
        is held on its own, under its item: a stale block as ``VIEW_CONFLICT``, an invalid one
        as ``VIEW_INVALID``, a block naming no row as ``VIEW_UNBOUND``, and a removed one as
        ``VIEW_DELETED``. Bytes held whole, displaced ones included, are governed by every row
        the log shows. Either way the human's bytes are in the store or in a hold before the
        install may displace them.
        """
        batch = self._publication
        path = projection["path"]
        digest = hashlib.sha256(raw).hexdigest()
        observed = None if batch.observed is None else batch.observed.get(path)
        if slot != "offline":
            found = edit_back.Classification(edit_back.CONFLICT)  # A1: an unseen edit met this publication
        elif not batch.edit_back or (batch.observed is not None and (observed is None or observed.digest != digest)):
            batch.pending = True
            return False
        else:
            found = edit_back.classify(self, projection, raw, refusal=observed.refusal if observed else ())
        if found.code is not None:
            if found.code == edit_back.FOREIGN:
                batch.foreign_discovered = True
            holds = [(found, raw, edit_back.held_id(path, raw=raw), edit_back.whole_log_candidate(), None)]
        elif (found.frame is not None and found.frame.code is None) or any(block.code is None for block in found.blocks):
            batch.pending = True  # edit-back applies the edited blocks once the view settles
            return False
        else:
            holds = [(block, block.raw, edit_back.held_id(block.path, base=block.current)
                      if block.code == edit_back.DELETED else edit_back.held_id(block.path, raw=block.raw),
                      {"action": "view-edit", "item_key": self._log_block_key(projection, block.path)}, block.path)
                     for block in found.blocks if block.code != edit_back.SAME]
        for held, held_raw, reference, candidate, block_path in holds:
            existing = self.connection.execute("SELECT held_bytes FROM held_candidates WHERE held_id=?",
                                               (reference,)).fetchone()
            if existing is not None and existing[0] == held_raw:
                continue  # these bytes are held once
            diagnostics = {
                "code": held.code, "path": path, "token": token, "slot": slot,
                "sha256": hashlib.sha256(held_raw).hexdigest(), "stamp": held.stamp,
                "base_row_version": held.base, "current_row_version": held.current,
                **({"block": block_path} if block_path else {}),
                **({"issues": list(held.issues)} if held.issues else {}),
            }
            if not self._hold_view_correction(projection, held.code, held_raw, reference, diagnostics, held.base,
                                              source, candidate=candidate):
                return False
        batch.accept(path, digest)
        return True

    def _log_block_key(self, projection, block_path):
        """The item key of a log block's view path ``<log>#<key>``, or None for a block naming no row."""
        row = self.connection.execute("SELECT item_key FROM items WHERE collection_id=? AND view_path=?",
                                      (projection["collection_id"], block_path)).fetchone()
        return None if row is None else row[0]

    def _hold_view_correction(self, projection, code, raw, reference, diagnostics, base, source, *, candidate=None):
        """Hold one view correction, governed by the displaced render context, never the human's bytes.

        ``candidate`` names what the hold would change, by default the view's own item; a
        whole log view names every row it shows (``governance.held_targets``).
        """
        contexts = (source or {}).get("source_contexts", [])
        pairs = views.source_pairs(source or {})
        metadata = set()
        covered = set()
        try:
            for context in contexts:
                if not isinstance(context, dict):
                    metadata.clear()
                    covered.clear()
                    break
                pair = (context.get("row_version"), context.get("sha256"))
                if pair not in pairs or any(context.get(name) != projection.get(name)
                                           for name in ("path", "collection_id", "kind", "row_id")):
                    continue
                metadata.add(_json(governance._metadata(context["governance_json"])))
                covered.add(pair)
        except (KeyError, TypeError, ValueError):
            metadata.clear()
        if not pairs or covered != pairs or len(metadata) != 1:
            # Ambiguous historical authority preserves bytes under the existing owner-only quarantine floor.
            canonical = self.connection.execute(
                "SELECT m.governance_json FROM collection_manifests m JOIN collections c "
                "ON c.collection_id=m.collection_id AND c.manifest_version=m.manifest_version WHERE c.collection_id=?",
                (projection["collection_id"],),
            ).fetchone()[0]
            return self._store_view_correction(projection, code, raw, reference, diagnostics, base,
                                               canonical, candidate=candidate, quarantined=True)
        metadata = metadata.pop()
        return self._store_view_correction(projection, code, raw, reference, diagnostics, base,
                                           metadata, candidate=candidate)

    def _store_view_correction(self, projection, code, raw, reference, diagnostics, base, metadata,
                               *, candidate=None, quarantined=False):
        """Persist a correction after the caller derives its canonical authority."""
        # Summary render bytes contain owner-only counts and basis; their held bytes retain that floor.
        quarantined = quarantined or projection["kind"] == "summary"
        _, manifest, _ = self._collection(projection["collection_id"])
        path = f"{records._held_directory(manifest)}/{reference}.md"
        now = dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        self._publication.capture_previous(path)
        if candidate is None:
            target = None
            if projection["kind"] == "item":
                target = self.connection.execute("SELECT item_key FROM items WHERE row_id=?",
                                                 (projection["row_id"],)).fetchone()
            candidate = {"action": "view-edit", "item_key": target[0] if target else None}
        diagnostics = _json([diagnostics])
        self._execute(
            "INSERT INTO held_candidates(held_id,collection_id,kind,code,candidate_json,held_bytes,"
            "diagnostics_json,view_path,base_row_version,updated_at,governance_json,governance_hash,quarantined) "
            "VALUES (?,?,'view-correction',?,?,?,?,?,?,?,?,?,?) ON CONFLICT(held_id) DO UPDATE SET "
            "code=excluded.code,candidate_json=excluded.candidate_json,held_bytes=excluded.held_bytes,"
            "diagnostics_json=excluded.diagnostics_json,base_row_version=excluded.base_row_version,"
            "updated_at=excluded.updated_at,governance_json=excluded.governance_json,"
            "governance_hash=excluded.governance_hash,quarantined=excluded.quarantined",
            (reference, projection["collection_id"], code, _json(records._encode_held_value(candidate)), raw, diagnostics, path,
             base if type(base) is int else None, now, metadata, hashlib.sha256(raw).hexdigest(), int(quarantined)),
        )
        text = views.held_view({
            "collection_id": projection["collection_id"], "held_id": reference,
            "updated_at": now, "kind": "view-correction", "held_bytes": raw,
            "diagnostics_json": diagnostics, "governance_json": metadata,
        }, self._publication.identity)
        self._pending(path, projection["collection_id"], "held", 1, text, manifest=manifest)
        self.handle.release_cache.touch(projection["collection_id"],
                                       f"exomem://collection-held/{projection['collection_id']}/{reference}")
        if code == edit_back.FOREIGN:
            self._execute("INSERT OR REPLACE INTO store_meta(key,value) VALUES (?,'1')", (schema.META_VIEW_DIVERGED,))
        self._precommit(manifest)
        return True

    def _hold_deleted(self, projection, source):
        """Hold a deleted item or manifest view, which no delete action can stand for (design §6)."""
        current = edit_back.current_version(self.connection, projection)
        reference = edit_back.held_id(projection["path"], base=current)
        if self.connection.execute("SELECT 1 FROM held_candidates WHERE held_id=?", (reference,)).fetchone():
            return True
        # A deleted log view stood for every row it showed.
        candidate = edit_back.whole_log_candidate() if projection["kind"] == "log" else None
        return self._hold_view_correction(projection, edit_back.DELETED, b"", reference, {
            "code": edit_back.DELETED, "path": projection["path"], "base_row_version": current,
            "current_row_version": current,
        }, current, source, candidate=candidate)

    def _detached_values(self, manifest, path, raw):
        """An unbound append proposal; only the guarded append leaf validates or adopts it."""
        from .legacy_import import _body

        values, body, key = {}, "", None
        try:
            frontmatter, body, _ = vault.parse_frontmatter(record_formats._decode_item_bytes(raw), strict=True)
            if isinstance(frontmatter, Mapping):
                profile = record_formats.profile_for(manifest.semantic_profile)
                # These are the Markdown item protocol's system properties, not customer vocabulary.
                system = {"type", "collection_id", "schema_version", "exomem_view", profile.item_id_property}
                values = {name: record_formats._json_value(value) for name, value in frontmatter.items()
                          if name not in system}
                key = memory_refs.normalize_id(frontmatter.get(profile.item_id_property))
                body = _body(manifest, values, body)
        except (UnicodeError, ValueError, collections.CollectionError):
            pass
        return {"action": "append", "item": values, "body": body, "item_key": key, "source_path": path}

    def _require_detached_path(self, manifest, path, item=None):
        """Check the proposed path against canonical metadata, never detached frontmatter."""
        subjects = governance.iter_subjects(self.connection, manifest.collection_id, "unbound",
            **({"identity": manifest.path} if item is None else {"row_key": item["item_key"]}))
        try:
            subject = next(subjects)
        finally:
            subjects.close()
        basis = replace(subject.basis, subject=replace(subject.basis.subject, path=path))
        if self._operation.decision(replace(subject, basis=basis)).level < 6:
            self._operation.refuse()

    def _detached_path_occupied(self, cid, path, *, previous=None):
        key = collections._portable_path_key(path)
        occupied = self.handle.release_cache.state(cid).occupied_path_keys.get(key, 0)
        if previous is not None and collections._portable_path_key(previous) == key:
            occupied -= 1
        return occupied > 0 or self.connection.execute(
            "SELECT 1 FROM projection_state WHERE path=? UNION ALL SELECT 1 FROM items WHERE view_path=? LIMIT 1",
            (path, path)).fetchone() is not None

    def _hold_detached(self, manifest, path, raw, code, *, item=None, quarantined=True,
                       token=None, slot=None):
        """Detached input gets canonical policy plus an owner floor when provenance is unresolved."""
        projection = {"path": path, "collection_id": manifest.collection_id, "kind": "item",
                      "row_id": None if item is None else item["row_id"]}
        metadata = (item["governance_json"] if item is not None else self.connection.execute(
            "SELECT governance_json FROM collection_manifests WHERE collection_id=? AND manifest_version="
            "(SELECT manifest_version FROM collections WHERE collection_id=?)",
            (manifest.collection_id, manifest.collection_id)).fetchone()[0])
        candidate = (self._detached_values(manifest, path, raw) if item is None else
                     {"action": "view-edit", "item_key": item["item_key"], "source_path": path})
        reference = edit_back.held_id(path, raw=raw)
        existing = self.connection.execute(
            "SELECT held_bytes,quarantined,governance_json FROM held_candidates WHERE held_id=?", (reference,)).fetchone()
        if existing == (raw, int(quarantined), metadata):
            return True
        return self._store_view_correction(
            projection, code, raw, reference,
            {"code": code, "path": path, "sha256": hashlib.sha256(raw).hexdigest(),
             **({"token": token, "slot": slot} if token is not None else {})},
            None if item is None else item["row_version"], metadata,
            candidate=candidate, quarantined=quarantined)

    def reconcile_detached_candidate(self, facts, *, census):
        """Recheck one completed sweep's candidate, then hold it or record one metadata-only move."""
        from . import legacy_originals

        path, cid = facts["path"], facts["collection_id"]
        vault._safe_guard_target(path)
        with held_fs.acquire(self.root).require() as fs, fs.parent(str(Path(path).parent)).require() as parent:
            with fs.file(parent, Path(path).name).require() as file:
                version = file.version()
                raw = fs.read(file, max_bytes=record_formats._MAX_ITEM_BYTES).require()
                if (file.version() != version or asdict(version) != facts["version"]
                        or hashlib.sha256(raw).hexdigest() != facts["digest"]):
                    raise held_fs.HeldFsError("INPUT_CHANGED", "detached candidate changed")
            fs.validate_directory(parent).require()
        observed = census.db.execute("SELECT generation,manifest FROM temp.roots WHERE cid=? AND complete=1", (cid,)).fetchone()
        if observed is None:
            raise connection.CollectionStoreError("COLLECTION_STORE_PROJECTION_PENDING", "view sweep is incomplete")
        result = {"moved": False}
        with self._mutation(reconcile=True):
            row, manifest, _ = self._collection(cid)
            outside = facts.get("old_path") is not None
            if (row["generation"] != observed[0] or not _renders_rows(manifest)
                    or (not outside and not path.startswith(manifest.storage.source + "/"))):
                raise connection.CollectionStoreError("COLLECTION_STORE_PROJECTION_PENDING", "view sweep basis changed")
            if not self._publication.eligible({"collection_id": cid, "path": manifest.path}):
                raise connection.CollectionStoreError("COLLECTION_STORE_PROJECTION_PENDING", "collection route is closed")
            item = self._item(cid, facts["item_key"]) if facts["item_key"] is not None else None
            self._require_detached_path(manifest, path, item)
            projection = (None if item is None else self._publication.previous(item["view_path"]))
            if outside and (projection is None or projection["path"] != facts["old_path"]):
                raise connection.CollectionStoreError("COLLECTION_STORE_PROJECTION_PENDING", "moved source basis changed")
            stamp, identity = facts["stamp"], self._publication.identity
            lineage = {entry["instance_id"] for entry in json.loads(self.connection.execute(
                "SELECT value FROM store_meta WHERE key='lineage'").fetchone()[0])}
            code = edit_back.MOVED if outside else edit_back.UNBOUND if item is None else edit_back.CONFLICT
            current = bool(item is not None and projection is not None and stamp is not None
                           and stamp["s"] == identity["store_id"] and stamp["i"] in lineage
                           and stamp["v"] == item["row_version"] and stamp["h"] == item["payload_hash"][:12])
            legacy = None if projection is None or stamp is not None else legacy_originals.view_basis(
                self.connection, projection)
            legacy_exact = legacy is not None and legacy[0] == facts["digest"]
            current = current or legacy_exact
            if stamp is not None and (stamp["s"] != identity["store_id"] or stamp["i"] not in lineage):
                code = edit_back.FOREIGN
            elif item is not None and not current:
                code = edit_back.CONFLICT if stamp and stamp["v"] < item["row_version"] else edit_back.INVALID
            elif sync_providers.is_conflict_copy(self.root, Path(path).name):
                code = edit_back.CONFLICT_COPY
            canonical = None
            if projection is not None:
                _version, text = self._render_view(projection, manifest)
                canonical = text.encode()
            # Persisted inode numbers can be reused; only complete canonical bytes prove detached release authority.
            exact = canonical == raw
            duplicate = len(census.db.execute(
                "SELECT path FROM temp.candidates WHERE cid=? AND item_key=? AND conflict=0 "
                "AND json_extract(stamp,'$.s')=? AND json_extract(stamp,'$.i')=? "
                "AND json_extract(stamp,'$.v')=? AND json_extract(stamp,'$.h')=? LIMIT 2",
                (cid, facts["item_key"], *(stamp[name] for name in ("s", "i", "v", "h")))
            ).fetchall()) if current and stamp is not None else 0
            if legacy_exact:
                duplicate = len(census.db.execute(
                    "SELECT path FROM temp.candidates WHERE cid=? AND item_key=? AND conflict=0 "
                    "AND json_type(stamp)='null' LIMIT 2",
                    (cid, facts["item_key"])).fetchall())
            old_guard = None
            if not outside and current and duplicate == 1 and not sync_providers.is_conflict_copy(self.root, Path(path).name):
                try:
                    old_guard = vault.PathGuard.capture(self.root, projection["path"], leaf_policy="absent")
                except vault.PathGuardError:
                    pass  # The canonical file is still present: this is a copy, never a move.
            occupied = self._detached_path_occupied(cid, path, previous=item["view_path"] if item else None)
            if old_guard is not None and not occupied:
                guard = self._publication.retire_moved_inputs(
                    projection, path,
                    preserve=lambda data, token, slot: self._hold_detached(
                        manifest, path, data, edit_back.CONFLICT, item=item, token=token, slot=slot))
                if guard is None:
                    fresh, guard = vault.read_bounded_guarded_bytes(self.root, path, limit=record_formats._MAX_ITEM_BYTES,
                                                                   expected_hash=facts["digest"])
                else:
                    fresh = guard.raw
                if fresh != raw or (guard.leaf_identity.device, guard.leaf_identity.inode) != (version.identity.device, version.identity.inode):
                    raise held_fs.HeldFsError("INPUT_CHANGED", "moved candidate changed")
                self._recheck_guard(old_guard)
                self._recheck_guard(guard)
                if not exact and not legacy_exact:
                    self._hold_detached(manifest, path, raw, edit_back.CONFLICT, item=item)
                if legacy_exact and not legacy[1]:
                    legacy_originals.put(self.connection, cid, "item", facts["item_key"], projection["path"], raw,
                                         legacy_originals.checkpoint(self.connection, cid))
                arguments = {"old_path": projection["path"], "path": path, "generation": row["generation"],
                             "version": item["row_version"], "digest": facts["digest"]}
                request = hashlib.sha256(b"exomem-view-move:v1\0" + _json(arguments).encode()).hexdigest()
                request, digest, replay = self._identity("view_move", cid, arguments, request)
                if replay is not None:
                    return result
                # Immutable receipt/effect facts reconstruct audit; request identity still binds the discarded formatting digest.
                effects = {name: arguments[name] for name in ("old_path", "path", "generation", "version")}
                txn = self._txn(row, manifest, "view_move", edit_back.why(path), request, digest, effects=effects)
                self._execute("UPDATE items SET view_path=? WHERE row_id=?", (path, item["row_id"]))
                self._execute("UPDATE projection_state SET path=?,install_json=NULL,stat_identity=NULL WHERE path=?",
                              (path, projection["path"]))
                moved = self._publication.previous(path)
                moved["source_pairs"], moved["source_contexts"] = [], []
                self._publication.captured[path] = moved
                self._publication.recovered_inputs[path] = []
                if exact:
                    # This context describes a canonical render, never the observed detached bytes.
                    context = self._publication._render_context(moved)
                    moved["source_pairs"] = [[context["row_version"], context["sha256"]]]
                    moved["source_contexts"] = [context]
                    descriptor = {"token": uuid.uuid4().hex, "source_pairs": moved["source_pairs"],
                                  "source_contexts": [context], "render_context": context}
                    moved["install_json"] = _json(descriptor)
                    self._execute("UPDATE projection_state SET install_json=? WHERE path=?", (moved["install_json"], path))
                else:
                    descriptor = {"token": uuid.uuid4().hex, "source_pairs": [], "unresolved_target_provenance": True}
                    moved["install_json"] = _json(descriptor)
                    self._execute("UPDATE projection_state SET install_json=? WHERE path=?", (moved["install_json"], path))
                    self._publication.accept(path, facts["digest"])
                    self._pending(path, cid, "item", item["row_version"], canonical.decode(), item["row_id"], manifest=manifest)
                self._execute(tables.INSERT_EFFECT, {
                    "txn_id": txn["txn_id"], "ordinal": 0, "row_id": item["row_id"], "item_key": item["item_key"],
                    "effect": "update", "effect_label": "move", "version_before": item["row_version"],
                    "version_after": item["row_version"], "hash_before": item["payload_hash"], "hash_after": item["payload_hash"]})
                after = self._advance(txn)
                receipt = records._result(operation="update", manifest=manifest, key=item["item_key"],
                    before_item_hash=self._version(item), after_item_hash=self._version(item),
                    before_container_hash=self._container(row), after_container_hash=after,
                    affected_paths=[projection["path"], path], payload_hash=None,
                    outcome="committed", audit_correlation=txn["transition_id"])
                self._history_reselection(cid)
                self.handle.release_cache.touch(cid, f"exomem://{types.type_for_manifest(manifest).item_type}/{cid}/{item['item_key']}")
                self._precommit(manifest)
                self._insert_txn(txn, receipt)
                result = {"moved": True, "old_path": projection["path"],
                          "generation": txn["generation_after"]}
            else:
                _fresh, guard = vault.read_bounded_guarded_bytes(self.root, path, limit=record_formats._MAX_ITEM_BYTES,
                                                                expected_hash=facts["digest"])
                self._recheck_guard(guard)
                self._hold_detached(manifest, path, raw, code, item=item, quarantined=not exact)
        if result["moved"]:
            census.db.execute("UPDATE temp.roots SET generation=? WHERE cid=?", (result["generation"], cid))
        return result

    def _resolve_in_place(self, path, base):
        """Release the holds of earlier bytes at a view path once that path is valid again."""
        cursor = self.connection.execute(
            "SELECT * FROM held_candidates WHERE kind='view-correction' AND held_id IN (?,?)",
            (edit_back.held_id(path, base=base), edit_back.held_id(path, base=None)),
        )
        names = [column[0] for column in cursor.description]
        for held in cursor.fetchall():
            self._release(dict(zip(names, held, strict=True)))

    def _recover_view_inputs(self, batch, previous):
        descriptor = json.loads(previous["install_json"])
        parent = batch._parent(previous["path"])
        preserved = []
        for displaced in [descriptor, *descriptor.get("preserved", [])]:
            expected = {pair[1] for pair in views.source_pairs(displaced)} | set(displaced.get("accepted", ()))
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
            ready = descriptor.get("install_ready") or (views.source_pair(previous, "pending")
                                                        if "source_pairs" not in descriptor else None)
            if ready is not None and digest == ready[1]:
                if link_count == 2:
                    batch.pair_cleanup[previous["path"]] = (parent, stage_leaf, digest)
                else:
                    batch.cleanup.append((previous, parent, stage_leaf, digest))
            else:
                source = {"source_pairs": [ready] if ready is not None else [],
                          "source_contexts": [descriptor.get("render_context", {})]}
                if not self._register_view_input(previous, raw, descriptor["token"], 2, source=source):
                    raise connection.CollectionStoreError(
                        "COLLECTION_STORE_PROJECTION_PENDING", "staged view edit awaits governed preservation")
                batch.cleanup.append((previous, parent, stage_leaf, digest))
        elif result.error.code != "MISSING":
            raise result.error
        return preserved

    def reconcile_views(self, *, limit=64, after=None, pending_only=False, paths=None, missing_ready=None,
                        stop: Callable[[], bool] | None = None):
        """Recover one path window; inspect never writes or invokes this path.

        The window does not bound file bytes, preserved inputs or directory enumeration.
        ``pending_only`` takes only views awaiting publication, as the serving store thread does;
        under the edit-back release it leaves an edit at an item or manifest view path to
        edit-back, which waits for the edit to settle.
        ``paths`` maps each view edit-back settled to its observation; only those are visited,
        and only while their bytes are still the observed ones.
        ``stop`` leaves further optional work pending between complete recovery and publication units;
        filesystem calls and transaction finalization still finish.
        """
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 1024:
            raise ValueError("reconcile limit must be between 1 and 1024")
        if after is not None and not isinstance(after, str):
            raise ValueError("reconcile continuation must be a path")
        result = {"examined": 0, "warnings": [], "next_path": after or ""}
        if stop is not None and stop():
            result["warnings"].append("projection_pending")
            return result

        def awaits_census(projection):
            # A partial census cannot recreate an old path before finding an offline rename; bytes that
            # never reached the path left no old path to find.
            return (projection["kind"] == "item" and missing_ready is not None
                    and not missing_ready(projection["collection_id"]) and views.may_have_reached_path(projection))

        with self._mutation(reconcile=True, stop=stop):
            batch = self._publication
            batch.bind(result)
            batch.observed = paths if paths is not None else {} if pending_only and batch.edit_back else None
            result["recovered_stages"] = batch.recovered_stages
            if batch.stopped():
                return result
            if paths is not None:
                cursor = self.connection.execute(
                    "SELECT * FROM projection_state WHERE kind IN ('item','manifest','held','summary','log','history') "
                    f"AND path IN ({','.join('?' for _ in paths)}) ORDER BY path LIMIT ?", (*paths, limit + 1))
            else:
                cursor = self.connection.execute(
                    "SELECT * FROM projection_state WHERE kind IN ('item','manifest','held','summary','log','history') AND path>? "
                    "AND (? OR state='pending') ORDER BY path LIMIT ?",
                    (after or "", not pending_only, limit + 1),
                )
            rows = [dict(zip((c[0] for c in cursor.description), row, strict=True)) for row in cursor.fetchall()]
            more = len(rows) > limit
            if more:
                rows = rows[:limit]
            visited_parents = set()
            history_pages = {}
            refused = {}
            sweep = self.handle.release_cache.queued_history_sweep()
            if sweep is not None:
                refused[sweep.key[0]] = self._publication_refused(sweep.key[0])
                if refused[sweep.key[0]]:
                    # One sweep serves the whole store: a collection that cannot publish must not hold it from the others.
                    self.handle.release_cache.stage_history_sweep(None)
            consumed = after or ""
            for projection in rows:
                if batch.stopped():
                    result["next_path"] = consumed
                    break
                preceding, consumed = consumed, projection["path"]
                if not batch.eligible(projection):
                    continue
                if projection["collection_id"] not in refused:
                    refused[projection["collection_id"]] = self._publication_refused(projection["collection_id"])
                if refused[projection["collection_id"]]:
                    # Fail closed for that collection alone: its views stay pending, and its refusal must not stop the others.
                    batch.pending = True
                    continue
                _, manifest, _ = self._collection(projection["collection_id"])
                if projection["kind"] == "history":
                    from . import history

                    if manifest.collection_id not in history_pages:
                        context = history.PageContext(self.connection, self.root, manifest,
                                                      cache=self.handle.release_cache)
                        discovered = history.queue_pages(self, manifest, stopped=batch.stopped, limit=limit)
                        requested = {row["path"] for row in rows
                                     if row["kind"] == "history" and row["collection_id"] == manifest.collection_id}
                        if not discovered:
                            self._execute("UPDATE projection_state SET state='pending' WHERE path=?",
                                          (history.main_path(manifest),))
                        if batch.stopped():
                            result["next_path"] = preceding
                            break
                        rendered = context.render_many(requested)
                        history_pages[manifest.collection_id] = context, rendered, discovered
                        batch.history_contexts[(manifest.collection_id, manifest.manifest_version.hash)] = context
                    history_context, rendered, discovered = history_pages[manifest.collection_id]
                    history_version, history_text = rendered[projection["path"]]
                    history_digest = hashlib.sha256(history_text.encode()).hexdigest()
                    keep_history_pending = not discovered and projection["path"] == history.main_path(manifest)
                else:
                    keep_history_pending = False
                batch.precommits[manifest.collection_id] = manifest
                result["examined"] += 1
                try:
                    if awaits_census(projection):
                        try:
                            parent = batch._parent(projection["path"])
                            opened = batch._fs().file(parent, Path(projection["path"]).name)
                            if opened.ok:
                                opened.require().close()
                            elif opened.error.code == "MISSING":
                                batch.pending = True
                                continue
                            else:
                                raise opened.error
                        except held_fs.HeldFsError as error:
                            if error.code != "MISSING":
                                raise
                            batch.pending = True
                            continue
                    parent_path = Path(projection["path"]).parent
                    if parent_path not in visited_parents:
                        batch._parent(projection["path"], create=True)
                        batch.recover_orphans(projection["path"], limit=limit)
                        visited_parents.add(parent_path)
                        if batch.stopped():
                            result["next_path"] = preceding
                            break
                    projection = batch.capture_previous(projection["path"])
                    if batch.stopped():
                        result["next_path"] = preceding
                        break
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
                    if raw is None and awaits_census(projection):
                        batch.pending = True
                        continue
                    observed = None if paths is None else paths[projection["path"]]
                    if observed is not None and digest != observed.digest:
                        continue  # changed again since it settled; the next pass looks again
                    if projection["kind"] == "history" and not history_context.exists(projection["path"]):
                        if raw is None and not preserved:
                            self._execute("DELETE FROM projection_state WHERE path=?", (projection["path"],))
                        else:
                            batch.retire(projection)
                        continue
                    current_render = projection["kind"] != "history" or digest == history_digest
                    if digest is not None and digest == projection["pending_sha256"] and current_render:
                        if not preserved and not keep_history_pending:
                            if not batch.confirm_target(projection):
                                batch.pending = True
                                continue
                            self._execute(
                                "UPDATE projection_state SET published_row_version=pending_row_version,"
                                "published_sha256=pending_sha256,pending_row_version=NULL,pending_sha256=NULL,"
                                "state='current' WHERE path=?", (projection["path"],)
                            )
                        continue
                    if (digest is not None and digest == projection["published_sha256"]
                            and (projection["state"] != "pending" or projection["kind"] == "history")
                            and current_render):
                        if projection["kind"] == "history" and projection["state"] == "pending" and not keep_history_pending:
                            self._execute("UPDATE projection_state SET state='current',pending_row_version=NULL,"
                                          "pending_sha256=NULL WHERE path=?", (projection["path"],))
                        if projection["state"] == "held":
                            # The owner restored the published view, which resolves its in-place hold.
                            self._resolve_in_place(projection["path"], edit_back.current_version(self.connection, projection))
                            self._execute("UPDATE projection_state SET state='current' WHERE path=?", (projection["path"],))
                        continue
                    source = {
                        "source_pairs": projection["source_pairs"],
                        "source_contexts": projection["source_contexts"],
                    }
                    own = {projection["published_sha256"], projection["pending_sha256"],
                           *batch.accepted.get(projection["path"], ())}
                    if raw is not None and digest not in own:
                        token = json.loads(projection["install_json"])["token"] if projection["install_json"] else uuid.uuid4().hex
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
                    if projection["path"] in batch.kept:
                        # O2: the owner's held bytes stay in place until the hold is resolved.
                        self._execute("UPDATE projection_state SET state='held',stat_identity=? WHERE path=?",
                                      (observed.stat if observed else None, projection["path"]))
                        continue
                    if (raw is None and batch.edit_back and projection["kind"] in edit_back.EDITABLE_KINDS
                            and not preserved and projection["published_sha256"] is not None
                            and (batch.observed is None or projection["path"] in batch.observed)):
                        # A deleted view is held, then re-rendered: no delete exists (design §6).
                        if not self._hold_deleted(projection, source):
                            batch.pending = True
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
                    version, text = ((history_version, history_text) if projection["kind"] == "history"
                                     else self._render_view(projection, manifest))
                    self._pending(projection["path"], projection["collection_id"], projection["kind"], version, text, projection["row_id"], manifest=manifest)
                except (held_fs.HeldFsError, OSError, connection.CollectionStoreError):
                    batch.pending = True
                self._precommit(manifest)
            else:
                sweep = self.handle.release_cache.queued_history_sweep()
                if sweep is not None:
                    from . import history

                    active_main = history.main_path(self._collection(sweep.key[0])[1])
                    previous = next((row["path"] for row in reversed(rows) if row["path"] < active_main), after or "")
                    result["next_path"] = min(consumed, previous)
                else:
                    result["next_path"] = consumed if more else None
        return result

    def _history_reselection(self, cid, *, new_row=False):
        changed = self.handle.release_cache.discard_history(cid, new_row=new_row)
        if not new_row or changed:
            self._execute("UPDATE projection_state SET state='pending' WHERE collection_id=? "
                          "AND kind='history' AND state!='pending'", (cid,))

    def invalidate_history_pages(self):
        """Queue derived history after its release inputs change; the normal drain does all publication."""
        from . import history

        # Read before the pages are marked: an input that moves later differs from this value at the next tick.
        inputs = history.release_inputs(self.root)
        with self._mutation(reconcile=True):
            self._execute("UPDATE projection_state SET state='pending' WHERE kind='history' AND state!='pending'")
            return inputs

    def apply_view_edit(self, observation, found):
        """Apply one settled, valid view edit as ONE governed update through the ordinary leaf.

        It is recorded as a ``view_edit`` transition by ``owner:view-edit``, with a request
        identity derived from the view path, its base version and its bytes, so a replay of
        the same bytes commits nothing new. A leaf refusal raises, and edit-back holds it.
        An edited log block (``found.path``) is one update of its row, identified by the
        block's own path and bytes.
        """
        projection, change, base = observation.projection, found.change, found.base
        if found.path is not None:
            identity = edit_back.request_id(found.path, found.base, hashlib.sha256(found.raw).hexdigest())
            # A log's blocks are separate updates of one collection, each guarded by its own row
            # version; the collection's guard moves with every block applied before it.
            row = self._collection_row(projection["collection_id"])
            change, base = {**change, "expected_container_hash": self._container(row)}, row["generation"]
        else:
            identity = edit_back.request_id(observation.path, found.base, observation.digest)
        self._view_edit = {"path": observation.path, "digest": observation.digest, "base": base,
                           "partial_log": projection["kind"] == "log"}
        try:
            if projection["kind"] == "manifest":
                return self.revise_collection(
                    projection["collection_id"], manifest_text=change["manifest_text"],
                    expected_manifest_hash=change["expected_manifest_hash"],
                    expected_container_hash=change["expected_container_hash"],
                    why=edit_back.why(observation.path), request_id=identity,
                )
            return self.update_record(
                projection["collection_id"], item_key=change["item_key"], changes=change["changes"],
                delete_fields=change["delete_fields"], body=change["body"], operation=change["operation"],
                expected_container_hash=change["expected_container_hash"],
                expected_item_version=change["expected_item_version"],
                why=edit_back.why(observation.path), hold=False, request_id=identity,
            )
        finally:
            self._view_edit = None

    def apply_view_frame(self, observation, found):
        """Commit only authenticated log prose; other buffer components still await their own leaves."""
        identity = edit_back.frame_request_id(observation.path, found.stamp, found.change["frame_hash"])
        with self._mutation():
            row, manifest, _ = self._collection(observation.projection["collection_id"])
            previous = self.request_receipt(identity)
            if previous is not None:
                return previous["receipt"]
            frame = log_view.load(row["log_frame_json"], manifest)
            if (manifest.storage.strategy != "markdown-log" or manifest.storage.source != observation.path
                    or not edit_back.authentic_frame_base(self, observation.projection, found.stamp)
                    or log_view.frame_hash(frame) != found.stamp["h"]):
                raise collections.CollectionError("VIEW_FRAME_CHANGED", "the log frame base changed")
            self._preflight_views([observation.path])
            txn = self._txn(row, manifest, "view_frame_edit", edit_back.why(observation.path), identity, identity)
            self._execute("UPDATE collections SET log_frame_json=? WHERE collection_id=?",
                          (found.change["frame_json"], manifest.collection_id))
            after = self._advance(txn)
            self._pending_log(manifest, txn["generation_after"])
            receipt = {key: txn[key] for key in ("operation", "collection_id", "transition_id", "commit_seq")}
            receipt.update(before_container_hash=self._container(row), after_container_hash=after)
            self._insert_txn(txn, receipt)
            self.handle.release_cache.touch(manifest.collection_id)
            self._precommit(manifest)
        return receipt

    def record_view_observations(self, observations):
        """Record the stat identity of views found holding their own bytes; no transition.

        The next pass then skips each such view with one stat. A view held in place whose
        owner restored its published bytes resolves that hold.
        """
        with self._mutation(reconcile=True):
            for observation in observations:
                from .legacy_originals import view_basis

                row = self.connection.execute(
                    "SELECT published_sha256,state FROM projection_state WHERE path=?", (observation.path,)
                ).fetchone()
                if row is None or row[0] != observation.digest or row[1] == "pending":
                    continue
                basis = view_basis(self.connection, observation.projection)
                if basis is not None and basis == (observation.digest, True):
                    # Admission already copied the originals; normal publication now stamps the imported view.
                    self._execute("UPDATE projection_state SET stat_identity=?,state='pending' WHERE path=?",
                                  (observation.stat, observation.path))
                    continue
                if row[1] == "held":
                    self._resolve_in_place(observation.path,
                                           edit_back.current_version(self.connection, observation.projection))
                self._execute("UPDATE projection_state SET stat_identity=?,state='current' WHERE path=?",
                              (observation.stat, observation.path))

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

    def migrate_typed_encoding(self, collection, *, limit=128, encoding=typed_storage.TYPED_V2) -> str:
        """Advance this collection's native encoding migration by one bounded batch.

        Internal maintenance under the writer lease and current authority, not
        a public route. Logical values, hashes, generations and audit are
        unchanged; the original encoding stays authoritative until the proved
        cutover commits. Returns ``building``, ``ready`` or ``failed``.
        """
        with self._mutation():
            _, manifest, _ = self._collection(collection)
            state = typed_storage.migrate_batch(
                index_migrations.AccountedWriter(self.connection, self._execute),
                manifest.collection_id, manifest.schema.fields, limit=limit, encoding=encoding,
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
        from .importer import saved_imports

        saved_imports(manifest, data)
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
        self._history_reselection(manifest.collection_id)
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
            try:
                connector_boundary.require_create(self.root, manifest.path)
                connector_boundary.require_create(self.root, manifest.storage.source)
            except ValueError as error:
                raise collections.CollectionError("WRITE_REFUSED", "target is unavailable") from error
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
                "manifest_version, generation, audit_head, audit_reader_version, created_txn, updated_txn, view_mode, "
                "derived) VALUES (?, ?, ?, ?, ?, ?, 1, 1, ?, 1, ?, ?, ?, ?)",
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
                    int(manifest.derived),
                ),
            )
            self._manifest(manifest, manifest_text, 1, txn["txn_id"])
            if manifest.view_mode == summary.SUMMARY:
                # An empty summary collection publishes its native payload/query pair here.
                if typed_storage.migrate_batch(index_migrations.AccountedWriter(self.connection, self._execute),
                                               manifest.collection_id, manifest.schema.fields, limit=1,
                                               encoding=typed_storage.TYPED_V2) != "ready":
                    raise RuntimeError("an empty collection publishes its typed layout at once")
                self._pending_summary(manifest, 1)
            affected = [manifest.path, *summary.page_paths(manifest)] + ([manifest.storage.source] if scaffold else [])
            log_text = None
            if scaffold and manifest.storage.strategy == "markdown-log":
                frame = log_view.empty(manifest)
                log_text = log_view.render(manifest, frame, ())
                self._execute(
                    "UPDATE collections SET log_frame_json = ? WHERE collection_id = ?",
                    (frame.as_json(), manifest.collection_id),
                )
                self._pending_log(manifest, 1)
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
        records._refresh_manifest_due_state(self.root, manifest)
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

    def _held(self, cid: str, held: str | None, *, corrections=False):
        """A held write refusal, or with ``corrections`` also a held view correction (discard).

        With ``corrections``, a view correction an earlier release held under its older id
        shape resolves too, so it can still be discarded.
        """
        if corrections and edit_back.legacy_held_id(held):
            reference, refusals = held, False
        else:
            reference, refusals = records._validate_held_reference(held), True
        if reference is None:
            return None
        row = _row(
            self.connection.execute(
                "SELECT * FROM held_candidates WHERE held_id = ? AND collection_id = ? "
                "AND ((kind = 'write-refusal' AND ?) OR (kind = 'view-correction' AND ?))",
                (reference, cid, refusals, corrections),
            )
        )
        if row is None:
            raise records._held_not_found()
        subject = self.handle.release_cache.point(f"exomem://collection-held/{cid}/{reference}")
        if subject is None or self._operation.decision(subject).level < 6:
            self._operation.refuse()
        row["candidate"] = records._decode_held_value(json.loads(row["candidate_json"]))
        return row

    def _detached_resume_path(self, manifest, resumed):
        """An explicit unbound resume may adopt its observed filename after fresh custody checks."""
        if resumed is None or resumed["kind"] != "view-correction" or resumed["code"] != edit_back.UNBOUND:
            return None
        if manifest.storage.strategy != "markdown-items":
            return None  # A held log block has no filename to adopt.
        candidate = resumed["candidate"]
        path = candidate.get("source_path")
        vault._safe_guard_target(path)
        if (not _renders_rows(manifest) or not path.startswith(manifest.storage.source + "/")
                or Path(path).suffix.lower() != ".md"):
            raise records._held_not_found()
        raw, guard = vault.read_bounded_guarded_bytes(self.root, path, limit=record_formats._MAX_ITEM_BYTES)
        self._require_detached_path(manifest, path)
        self._recheck_guard(guard)
        if raw != resumed["held_bytes"]:
            self._hold_detached(manifest, path, raw, edit_back.UNBOUND)
            raise collections.CollectionError("VIEW_INPUT_CHANGED", "detached source changed; both observations remain held")
        if self._detached_path_occupied(manifest.collection_id, path):
            raise collections.CollectionError("RECORD_ID_CONFLICT", "detached filename is already owned")
        self._publication.accept(path, hashlib.sha256(raw).hexdigest())
        return path

    def _rerender_discarded(self, held, manifest):
        """O2: discarding a correction held in place re-renders its view over the discarded bytes."""
        diagnostics = json.loads(held["diagnostics_json"] or "[]")
        path = diagnostics[0].get("path") if diagnostics and isinstance(diagnostics[0], dict) else None
        previous = self._publication.previous(path) if isinstance(path, str) else None
        if previous is None or previous["state"] != "held":
            return
        self._publication.accept(path, hashlib.sha256(held["held_bytes"]).hexdigest())
        previous = self._publication.capture_previous(path)
        if self._publication.recovered_inputs[path] is None:
            return  # the publication pass re-renders it once its inputs are recovered
        version, text = self._render_view(previous, manifest)
        self._pending(path, previous["collection_id"], previous["kind"], version, text, previous["row_id"],
                      manifest=manifest)

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

    def _pending_basis(self, path, cid, kind, version):
        """Commit an aggregate view's pending basis with the rows; reconcile renders and publishes it.

        Nothing renders on the acknowledgement path. The receipt says so: until reconcile, the
        published view shows an older basis.
        """
        if kind != "history":
            # History is always asynchronous; its queue is reported by inspect, without changing closed receipts.
            self._publication.pending = True
        self._confirm_installed(path)
        if kind == "log":
            previous = self._publication.previous(path)
            if previous is not None and previous["install_json"]:
                descriptor = json.loads(previous["install_json"])
                for name in ("published", "pending"):
                    descriptor["previous_" + name] = views.source_pair(previous, name)
                pairs = {tuple(pair) for name in ("published", "pending")
                         if (pair := views.source_pair(previous, name))}
                known = [*descriptor.get("source_contexts", []), descriptor.get("render_context")]
                # Retain recorded proof before clearing the digest; deriving it after a component write is too late.
                descriptor["source_contexts"] = list({_json(context): context for context in known
                    if isinstance(context, dict) and (context.get("row_version"), context.get("sha256")) in pairs}.values())
                self._execute("UPDATE projection_state SET install_json=? WHERE path=?", (_json(descriptor), path))
        self._execute(
            "INSERT INTO projection_state(path,collection_id,row_id,kind,pending_row_version,pending_sha256,state) "
            "VALUES (?,?,NULL,?,?,NULL,'pending') ON CONFLICT(path) DO UPDATE SET "
            "pending_row_version=excluded.pending_row_version,pending_sha256=NULL,state='pending'",
            (path, cid, kind, version),
        )

    def _pending_summary(self, manifest, generation):
        """The summary pages' pending basis (``_pending_basis``)."""
        for path in summary.page_paths(manifest):
            self._pending_basis(path, manifest.collection_id, "summary", generation)

    def _pending_log(self, manifest, generation):
        """The log view's pending basis (``_pending_basis``); the projector renders the whole log."""
        self._pending_basis(manifest.storage.source, manifest.collection_id, "log", generation)

    def _confirm_installed(self, path):
        """Record a page that a publication installed and no reconcile confirmed yet, before a new basis replaces it.

        Reconcile confirms an installed page on its next pass. A commit in between would
        otherwise clear the page's digest, so its bytes would match neither recorded digest
        and read as a foreign edit that never publishes again.
        """
        row = self._publication.previous(path)
        if row is None or row["pending_sha256"] is None:
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
        if views._digest(raw) == row["pending_sha256"] and batch.confirm_target(row):
            self._execute("UPDATE projection_state SET published_row_version=pending_row_version,"
                          "published_sha256=pending_sha256 WHERE path=?", (path,))

    def _item_write_context(self, manifest):
        manifest_metadata = self.connection.execute(
            "SELECT m.governance_json FROM collection_manifests m JOIN collections c "
            "ON c.collection_id=m.collection_id AND c.manifest_version=m.manifest_version WHERE c.collection_id=?",
            (manifest.collection_id,),
        ).fetchone()[0]
        encoding = typed_storage.collection_encoding(self.connection, manifest.collection_id)
        layout = None if encoding == typed_storage.JSON_V1 else typed_storage.require_layout(self.connection, manifest.collection_id)
        return manifest_metadata, encoding, layout

    def _item_parameters(self, manifest, key, values, body, path, txn, before, manifest_metadata, encoding):
        natural_key = _natural_key(manifest, values)
        payload = tokens.payload_hash(manifest.schema.version, key, values, body)
        version = 1 if before is None else before["row_version"] + 1
        metadata = governance.row_metadata(manifest.schema, values, manifest_metadata)
        # The collection is the encoding authority; typed payloads keep no JSON copy.
        stored = _json(values) if encoding == typed_storage.JSON_V1 else None
        parameters = {"natural_key": natural_key, "row_version": version, "values_json": stored,
                      "body": body, "payload_hash": payload, "updated_txn": txn["txn_id"],
                      "governance_json": metadata}
        if before is None:
            parameters.update(collection_id=manifest.collection_id, item_key=key,
                              schema_version=manifest.schema.version, view_path=path,
                              created_txn=txn["txn_id"], encoding=encoding)
        return parameters

    def _item_effect(self, manifest, key, txn, before, resumed, ordinal, row_id, version, payload):
        # The built-in v1 audit wire contract fixes Planning's replan label.
        return {"txn_id": txn["txn_id"], "ordinal": ordinal, "row_id": row_id, "item_key": key,
                "effect": "resume" if resumed else "insert" if before is None else "update",
                "effect_label": ("edit" if self._view_edit is not None and before else
                                 "replan" if manifest.semantic_profile == "planning" else "correction" if before else None),
                "version_before": None if before is None else before["row_version"], "version_after": version,
                "hash_before": None if before is None else before["payload_hash"], "hash_after": payload}

    def _write_item(self, manifest, key, values, body, path, txn, before, resumed, sources,
                    *, ordinal=0, queue_log=True, scope=None):
        if before is None and manifest.view_mode == "items":
            self._require_items_capacity(manifest.collection_id)
        if _renders_rows(manifest):
            self._publication.capture_previous(path)
        manifest_metadata, encoding, layout = self._item_write_context(manifest)
        parameters = self._item_parameters(manifest, key, values, body, path, txn, before, manifest_metadata, encoding)
        version, payload, stored = parameters["row_version"], parameters["payload_hash"], parameters["values_json"]
        if before is None:
            self._history_reselection(manifest.collection_id, new_row=True)
        elif parameters["governance_json"] != before["governance_json"] or path != before["view_path"]:
            self._history_reselection(manifest.collection_id)
        if before is None:
            cursor = self._execute(tables.INSERT_ITEM, parameters)
            row_id = cursor.lastrowid
        else:
            row_id = before["row_id"]
            self._execute(tables.UPDATE_ITEM, {**parameters, "target_row_id": row_id})
        if encoding == typed_storage.JSON_V1:
            # The JSON payload mints its json-v1 version_identity in the same statement.
            self._execute(tables.INSERT_VERSION, {
                "row_id": row_id, "row_version": version, "values_json": stored, "body": body,
                "payload_hash": payload, "txn_id": txn["txn_id"],
            })
        else:
            typed_storage.write_version(
                index_migrations.AccountedWriter(self.connection, self._execute),
                layout,
                row_id=row_id, row_version=version, values=values, body=body, payload_hash=payload,
                txn_id=txn["txn_id"], schema_version=manifest.schema.version, item_key=key, scope=scope,
            )
        index_migrations.maintain_item(index_migrations.AccountedWriter(self.connection, self._execute),
                                       manifest.collection_id, row_id, key, version, values,
                                       previous=json.loads(before["values_json"]) if before else None)
        for source_ordinal, source in enumerate(sources):
            self._execute(tables.INSERT_SOURCE, {"row_id": row_id, "row_version": version,
                          "ordinal": source_ordinal, "source_ref": source})
        self._execute(tables.INSERT_EFFECT,
                      self._item_effect(manifest, key, txn, before, resumed, ordinal, row_id, version, payload))
        if _renders_rows(manifest):
            _, text = self._render_view({"kind": "item", "row_id": row_id}, manifest)
            self._pending(path, manifest.collection_id, "item", version, text, row_id, manifest=manifest)
        elif queue_log and manifest.view_mode == summary.SUMMARY:
            self._pending_summary(manifest, txn["generation_after"])
        elif queue_log:
            self._pending_log(manifest, txn["generation_after"])
        self._release(resumed)
        self.handle.release_cache.touch(manifest.collection_id,
                                       f"exomem://{types.type_for_manifest(manifest).item_type}/{manifest.collection_id}/{key}")
        return tokens.item_version(manifest.collection_id, key, version, payload)

    def _write_new_items(self, manifest, plans, txn, *, scope=None):
        """Apply admitted new summary rows together inside the existing mutation transaction."""
        manifest_metadata, encoding, layout = self._item_write_context(manifest)
        self._history_reselection(manifest.collection_id, new_row=True)
        parameters = [self._item_parameters(manifest, key, values, body, path, txn, None, manifest_metadata, encoding)
                      for key, values, body, path, _, _ in plans]
        self._execute(tables.INSERT_ITEM, parameters)
        statement = select(tables.items.c.item_key, tables.items.c.row_id).where(
            tables.items.c.collection_id == bindparam("collection_id"),
            tables.items.c.item_key.in_(bindparam("item_keys", expanding=True)),
        )
        row_ids = dict(self._execute(statement, {"collection_id": manifest.collection_id,
                                               "item_keys": [row["item_key"] for row in parameters]}).fetchall())
        if encoding == typed_storage.JSON_V1:
            self._execute(tables.INSERT_VERSION, [{"row_id": row_ids[row["item_key"]], "row_version": 1,
                "values_json": row["values_json"], "body": row["body"], "payload_hash": row["payload_hash"],
                "txn_id": txn["txn_id"]} for row in parameters])
        else:
            typed_storage.write_new_versions(index_migrations.AccountedWriter(self.connection, self._execute), layout,
                [(row_ids[key], key, values, body, row["payload_hash"])
                 for (key, values, body, _, _, _), row in zip(plans, parameters, strict=True)],
                txn_id=txn["txn_id"], schema_version=manifest.schema.version, scope=scope)
        index_migrations.maintain_new_items(index_migrations.AccountedWriter(self.connection, self._execute),
            manifest.collection_id, [(row_ids[key], key, 1, values) for key, values, _, _, _, _ in plans])
        # Immediate source FKs require every payload identity to exist before sources attach.
        self._execute(tables.INSERT_SOURCE, [{"row_id": row_ids[key], "row_version": 1,
            "ordinal": 0, "source_ref": source} for key, _, _, _, _, source in plans])
        self._execute(tables.INSERT_EFFECT, [self._item_effect(manifest, key, txn, None, None, ordinal,
            row_ids[key], 1, row["payload_hash"])
            for ordinal, ((key, _, _, _, _, _), row) in enumerate(zip(plans, parameters, strict=True))])
        item_type = types.type_for_manifest(manifest).item_type
        for key, _, _, _, _, _ in plans:
            self.handle.release_cache.touch(manifest.collection_id, f"exomem://{item_type}/{manifest.collection_id}/{key}")

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
            self._rows_writable(manifest, "bulk_upsert")
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
                    values = (_import.prepared_row(index, manifest, reference, resolved, raw["item"])
                              if _import is not None else None)
                    if values is None:
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
            scope = self.handle.write_scope
            if manifest.view_mode == summary.SUMMARY and all(before is None for _, _, _, _, before, _ in plans):
                self._write_new_items(manifest, plans, txn, scope=scope)
            else:
                for ordinal, (key, values, body, path, before, source_rel) in enumerate(plans):
                    self._write_item(manifest, key, values, body, path, txn, before, None, [source_rel],
                                     ordinal=ordinal, queue_log=False, scope=scope)
            if is_log:
                self._pending_log(manifest, txn["generation_after"])
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
        # Reuse placement within this synchronous batch; each carrier still reads live state.
        with state_paths.resolution_scope():
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
        _prepare=None,
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
            self._rows_writable(manifest, "append")
            resumed = self._held(manifest.collection_id, held, corrections=True)
            if (resumed and resumed["kind"] == "view-correction" and manifest.storage.strategy == "markdown-log"
                    and resumed["candidate"].get("action") == "view-edit"):
                resumed = {**resumed, "candidate": edit_back.resumed_append(self, resumed, manifest)}
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
            if resumed and resumed["kind"] == "view-correction" and resumed["code"] != edit_back.UNBOUND:
                raise records._held_not_found()
            if item_key is None and resumed and resumed["kind"] == "view-correction":
                item_key = resumed["candidate"].get("item_key")
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
                detached_path = self._detached_resume_path(manifest, resumed)
            except collections.CollectionError as error:
                # A stale adoption commits its additional quarantine before returning the refusal.
                result = error if error.code == "VIEW_INPUT_CHANGED" else self._hold(
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
                if _prepare is not None and existing is not None:
                    # A prepared append always creates, as the file-mode preparation does.
                    raise collections.CollectionError("RECORD_ID_CONFLICT", "record key already exists")
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
                    if detached_path is not None:
                        path = detached_path
                    elif manifest.storage.strategy == "markdown-log":
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
                    if _prepare is not None:
                        # A curation seal asks what this append would commit, then stops before any row.
                        _prepare({"manifest": manifest, "item_key": key, "path": path, "payload_hash": payload,
                                  "body": body, "container_hash": before_container})
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
        _prepare=None,
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
            self._rows_writable(manifest, operation)
            key = records._validate_item_key(item_key, manifest=manifest, candidate=changes)
            before = self._item(manifest.collection_id, key)
            if before is None:
                raise collections.CollectionError("RECORD_NOT_FOUND", "record key does not exist")
            resumed = self._held(manifest.collection_id, held, corrections=True)
            if resumed:
                candidate = resumed["candidate"]
                if resumed["kind"] == "view-correction":
                    # A held stale-buffer save resumes as its bytes over the current row.
                    candidate = edit_back.resumed_update(self, resumed, manifest, before)
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
                    if (
                        not changes
                        or set(changes) - planning.TRIAGE_FIELDS
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
                if resumed and resumed["kind"] == "view-correction":
                    raise  # the correction keeps the owner's exact bytes; a refusal never rewrites them
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
                    with views.presentation_resolver(
                        self.connection, self.root, manifest, operation=self._operation,
                        observe_read=self._recheck_guard,
                    ) as resolver:
                        rendered = record_formats.render_markdown_item(
                            manifest, final, key, final_body, resolve_relationship=resolver,
                            view_stamp=views.stamp(self._publication.identity, before["row_version"], before["payload_hash"]),
                        )
                    if projection and projection[0] == hashlib.sha256(rendered.encode()).hexdigest():
                        raise collections.CollectionError(
                            "NOOP_RECORD_PRESENTATION", "managed presentation is already current"
                        )
                self._twins(manifest.collection_id, key, _natural_key(manifest, final))
                if _prepare is not None:
                    # A curation seal asks what this update would commit, then stops before any row.
                    _prepare({"manifest": manifest, "item_key": key, "path": before["view_path"],
                              "container_hash": self._container(row), "item_version": self._version(before)})
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

    def rebaseline_collection(self, collection, *, expected_manifest_hash, expected_container_hash,
                             acknowledged_gap_codes, why, request_id=None):
        """Acknowledge only a positively established imported discontinuity."""
        from . import history

        records._validate_why(why)
        with self._mutation():
            row, manifest, _ = self._collection(collection)
            records._expect_hash(expected_manifest_hash, manifest.manifest_version.hash, "manifest")
            records._expect_hash(expected_container_hash, self._container(row), "container")
            assessment = history.audit(self, row)
            gaps = tuple(assessment["gaps"])
            if (row["legacy_audit_status"] != "gap" or assessment["status"] != "gap" or not gaps
                    or not records._acknowledgeable_gap_codes(gaps)):
                raise collections.CollectionError("RECORD_AUDIT_GAP", "rebaseline requires an inspectable audit gap")
            codes = records._validate_gap_codes(tuple(acknowledged_gap_codes))
            if codes != gaps:
                raise collections.CollectionError("STALE_RECORD", "gap acknowledgement is stale")
            payload = records.lifecycle_request_hash(action="rebaseline", collection_id=manifest.collection_id,
                before_manifest_hash=expected_manifest_hash, before_container_hash=expected_container_hash,
                proposed_manifest_hash=None, acknowledged_gap_codes=codes, rationale=why)
            identity, digest, replay = self._identity("rebaseline", collection, {
                "expected_manifest_hash": expected_manifest_hash, "expected_container_hash": expected_container_hash,
                "acknowledged_gap_codes": codes, "why": why}, request_id)
            if replay is not None:
                return replay
            gap = records.lifecycle_gap_fingerprint(prior_head=row["audit_head"] or "baseline",
                acknowledged_gap_codes=codes, before_manifest_hash=expected_manifest_hash,
                before_container_hash=expected_container_hash)
            versions = [(manifest.path, manifest.manifest_version.hash), *(
                (path, tokens.item_version(manifest.collection_id, key, version, digest))
                for key, version, digest, path in self.connection.execute(
                    "SELECT item_key,row_version,payload_hash,view_path FROM items WHERE collection_id=? ORDER BY item_key",
                    (manifest.collection_id,)))]
            checkpoint = records.lifecycle_checkpoint_fingerprint(tuple(versions))
            facts = {"acknowledged_gap_codes": list(codes), "gap_fingerprint": gap,
                     "checkpoint_snapshot_hash": checkpoint}
            txn = self._txn(row, manifest, "rebaseline", why, identity, digest, effects=facts)
            after = self._advance(txn)
            self._execute("UPDATE collections SET audit_reader_version=2 WHERE collection_id=?", (manifest.collection_id,))
            receipt = records._lifecycle_result(operation="rebaseline", manifest=manifest,
                before_manifest_hash=expected_manifest_hash, after_manifest_hash=expected_manifest_hash,
                before_container_hash=expected_container_hash, after_container_hash=after, payload_hash=payload,
                audit_correlation=txn["transition_id"], continuity=False, acknowledged_gap_codes=codes,
                gap_fingerprint=gap, checkpoint_snapshot_hash=checkpoint, affected_paths=(manifest.path,))
            self._precommit(manifest)
            self._insert_txn(txn, receipt)
        return receipt

    def _prepare_revision(self, current, declared, manifest_text):
        """Validate the canonical proposal for both preflight and a guarded revision."""
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
        if proposed.derived != current.derived:
            raise derived_rows.conversion_refused(current.derived)
        if current.derived and summary.populated(self.connection, current.collection_id):
            refused = derived_rows.field_change_refused(current, proposed)
            if refused is not None:
                raise refused
        mode_change = proposed.view_mode != current.view_mode
        refused = summary.mode_change_refusal(self.connection, current, proposed)
        if refused is not None:
            raise refused
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
        metadata = governance.manifest_metadata(manifest_text)
        row_metadata = {key: governance.row_metadata(proposed.schema, values, metadata)
                        for key, values in plans.items()}
        return proposed, mode_change, plans, metadata, row_metadata

    def validate_revision(self, collection, *, manifest_text):
        """Return current guards for a valid proposal without writer admission or side effects."""
        with self.read_snapshot(mutation=True):
            row, current, declared = self._collection(collection)
            proposed, _mode, _plans, _metadata, _rows = self._prepare_revision(current, declared, manifest_text)
            return {"valid": True,
                    "lifecycle_guards": {"expected_manifest_hash": current.manifest_version.hash,
                                         "expected_container_hash": self._container(row)},
                    "normalized_contract": records._normalized_manifest_contract(proposed)}

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
            proposed, mode_change, plans, metadata, row_metadata = self._prepare_revision(
                current, declared, manifest_text)
            # Unchanged row inputs and governance retain their immutable original manifest's render proof.
            # Capture before replacing governance; held views lack that original-manifest chain.
            self._preflight_views([current.path, *(path for key, path, before in self.connection.execute(
                "SELECT item_key,view_path,governance_json FROM items "
                "WHERE collection_id=? AND view_path IS NOT NULL", (current.collection_id,),
            ) if row_metadata[key] != before), *(path for (path,) in self.connection.execute(
                "SELECT view_path FROM held_candidates WHERE collection_id=?", (current.collection_id,),
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
                        current.collection_id, proposed.schema.fields, limit=1,
                        encoding=typed_storage.TYPED_V2) != "ready":
                    raise RuntimeError("an empty collection publishes its typed layout at once")
            self._pending_summary(proposed, txn["generation_after"])
            self._execute(
                "UPDATE collections SET manifest_version = ?, audit_reader_version = 2 WHERE collection_id = ?",
                (txn["manifest_version_after"], current.collection_id),
            )
            self._execute("UPDATE items SET governance_json=? WHERE collection_id=? AND item_key=?",
                ((value, current.collection_id, key) for key, value in row_metadata.items()), many=True)
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
        records._refresh_manifest_due_state(self.root, proposed)
        return receipt

    def discard_held(self, collection, *, held, why):
        records._validate_why(why)
        with self._mutation():
            _, manifest, _ = self._collection(collection)
            candidate = self._held(manifest.collection_id, held, corrections=True)
            if candidate is None:
                raise records._held_not_found()
            self._precommit(manifest)
            self._release(candidate)
            if candidate["kind"] == "view-correction":
                self._rerender_discarded(candidate, manifest)
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
        writer_lease.mark_active_mutation_committed()
        return receipt
