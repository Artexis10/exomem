"""Canonical collection adapter for an explicitly bound dark store."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager, nullcontext
from dataclasses import replace
from typing import Any

from .. import call_spans, record_formats
from .. import structured_collections as collections
from . import governance, typed_storage


def row_source(manifest: collections.CollectionManifest, item: Mapping[str, Any]) -> str:
    """A row's source: its view file, the log, or a summary row's collection manifest."""
    if manifest.storage.strategy == "markdown-log":
        return manifest.storage.source
    return manifest.path if manifest.view_mode == "summary" else item["view_path"]


class StoreAdapter:
    mutable = True

    def __init__(
        self,
        writer: Any,
        manifest: collections.CollectionManifest,
        project_values: Callable[[Mapping[str, Any]], dict[str, Any]] | None,
    ) -> None:
        self.writer = writer
        self.manifest = manifest
        self.project_values = project_values
        self.deadline = None  # the Records query's (`begin_query`), shared by every read it makes

    def begin_query(self) -> None:
        """Bound this adapter's reads by one Records query deadline."""
        from ..query_engine import runtime

        self.deadline = runtime.Deadline(runtime.PROFILES["records"]["timeout_ms"])

    def _deadline_scope(self):
        if self.deadline is None:
            return nullcontext()
        from ..query_engine import runtime

        return runtime.deadline_scope(self.deadline)

    def read(self) -> record_formats.AdapterSnapshot:
        with self._deadline_scope(), self.writer.read_collection(self.manifest) as manifest:
            return self._read(manifest)

    def _read(self, manifest: collections.CollectionManifest) -> record_formats.AdapterSnapshot:
        with call_spans.span("store.authorized_rows"):
            items, snapshot, _ = self.writer._operation.authorized_rows(manifest.collection_id)
        items.sort(key=governance.input_order(manifest))
        with call_spans.span("store.materialize", {"items": len(items)}):
            return self._snapshot(manifest, items, snapshot)

    def _snapshot(self, manifest, items, snapshot):
        """Records of ``items``, in the order given."""
        versions = [manifest.manifest_version]
        if manifest.storage.strategy == "markdown-log":
            versions.append(collections.SourceVersion(manifest.storage.source, snapshot))
        fields = list(manifest.schema.fields)
        grammar = record_formats.log_grammar_tokens(manifest)
        if grammar is not None and grammar.note_field is not None and grammar.note_field not in fields:
            fields.append(grammar.note_field)
        records = []
        for item in items:
            source = collections.SourceVersion(row_source(manifest, item), item.get("public_version") or self.writer._version(item))
            stored = json.loads(item["values_json"])
            values = {name: stored[name] for name in fields if name in stored}
            identity = collections.ItemIdentity(manifest.collection_id, item["item_key"])
            if self.project_values is not None:
                with call_spans.span("store.project_values"):
                    values = self.project_values(values)
            records.append(record_formats.Record(
                identity,
                values,
                source, record_formats.SourceSpan(0, 0), body=item["body"],
            ))
            if manifest.storage.strategy == "markdown-items" and manifest.view_mode == "items":
                versions.append(source)
        return record_formats.AdapterSnapshot(
            records=tuple(records), snapshot=snapshot, data_snapshot=snapshot,
            source_versions=tuple(versions),
        )

    @contextmanager
    def selecting(self):
        """One read that answers a legacy query by selecting rows (`_Selecting`), or None.

        None where only the whole read answers as the caller sees it: a derived collection holds no
        rows here, a caller limited to some fields sees public versions and a digest snapshot, and
        typed v1 rows have no parity proof on the selecting scan.
        """
        from ..query_engine import runtime

        with self._deadline_scope(), self.writer.read_collection(self.manifest) as manifest:
            operation = self.writer._operation
            if manifest.derived or not operation.field_plan(manifest).owner:
                yield None
                return
            snapshot = operation.row_release(manifest.collection_id)[1]
            with runtime.read_session(self.writer.root, self.writer.handle.path,
                                      limits=runtime.QueryLimits(profile="records"),
                                      deadline=runtime.current_deadline()) as session:
                admitted = session.admit(manifest.collection_id)
                if admitted.layout is not None and admitted.layout.encoding != typed_storage.TYPED_V2:
                    yield None
                    return
                yield _Selecting(self, manifest, snapshot, admitted)

    def refuse_mutation(self, action: str) -> None:
        raise collections.CollectionError("UNSUPPORTED_RECORD_MUTATION", f"{action} is unsupported")


#: The ``items`` columns a record is rebuilt from: its values, version and input-order keys.
_RECORD_COLUMNS = ("row_id", "collection_id", "item_key", "row_version", "payload_hash", "values_json",
                   "body", "view_path", "created_txn", "encoding")


class _Selecting:
    """An owner's read of one collection on a query session, at the snapshot the whole read reports."""

    def __init__(self, adapter: StoreAdapter, manifest, snapshot: str, admitted) -> None:
        self.adapter, self.manifest, self.snapshot, self.admitted = adapter, manifest, snapshot, admitted
        self.fields = admitted.fields  # every top-level name a record's values can hold

    def select(self, query, *, lead: int = 0):
        """The `query_engine.sqlite.LegacySelection` answering ``query`` over the released rows."""
        from ..query_engine import sqlite

        return sqlite.execute_legacy(self.admitted, query, path=self.manifest.storage.source,
                                     format=self.manifest.storage.strategy, lead=lead)

    def records(self, row_ids: Sequence[int]) -> record_formats.AdapterSnapshot:
        """The records of ``row_ids`` in that order, built as the whole read builds each one.

        Its source versions list the manifest's, then the records' in input order, as the whole read does.
        """
        from ..query_engine import packed

        session = self.admitted.session
        conn = session.connection

        def read(part):
            session.check()
            sql = (f"SELECT {','.join(_RECORD_COLUMNS)} FROM items "  # noqa: S608 - fixed names, bound ids
                   f"WHERE row_id IN ({','.join('?' * len(part))})")
            return packed.rows(conn, sql, part, len(_RECORD_COLUMNS))

        def fetch(sql, parameters, width):
            session.check()
            return packed.rows(conn, sql, parameters, width)

        with call_spans.span("store.materialize", {"items": len(row_ids)}):
            items = [dict(zip(_RECORD_COLUMNS, row, strict=True))
                     for row in packed.in_chunks(read, list(row_ids), 128)]
            packed.in_chunks(lambda part: typed_storage.hydrate(conn, list(part), fetch=fetch), items, 128)
            items.sort(key=governance.input_order(self.manifest))
            parsed = self.adapter._snapshot(self.manifest, items, self.snapshot)
        by_key = {record.identity.key: record for record in parsed.records}
        keys = {item["row_id"]: item["item_key"] for item in items}
        return replace(parsed, records=tuple(by_key[keys[row_id]] for row_id in row_ids))
