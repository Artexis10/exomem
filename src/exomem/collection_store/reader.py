"""Canonical collection adapter for an explicitly bound dark store."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Any

from .. import record_formats
from .. import structured_collections as collections


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

    def read(self) -> record_formats.AdapterSnapshot:
        with self.writer.read_collection(self.manifest) as manifest:
            return self._read(manifest)

    def _read(self, manifest: collections.CollectionManifest) -> record_formats.AdapterSnapshot:
        items, snapshot, _ = self.writer._operation.authorized_rows(manifest.collection_id)
        return self._snapshot(manifest, items, snapshot)

    def _snapshot(self, manifest, items, snapshot):
        versions = [manifest.manifest_version]
        if manifest.storage.strategy == "markdown-log":
            items.sort(key=lambda item: (item["created_txn"], item["row_id"]),
                       reverse=manifest.storage.descriptor.get("insertion") == "newest-first")
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
            records.append(record_formats.Record(
                collections.ItemIdentity(manifest.collection_id, item["item_key"]),
                values if self.project_values is None else self.project_values(values),
                source, record_formats.SourceSpan(0, 0), body=item["body"],
            ))
            if manifest.storage.strategy == "markdown-items" and manifest.view_mode == "items":
                versions.append(source)
        return record_formats.AdapterSnapshot(
            records=tuple(records), snapshot=snapshot, data_snapshot=snapshot,
            source_versions=tuple(versions),
        )

    def refuse_mutation(self, action: str) -> None:
        raise collections.CollectionError("UNSUPPORTED_RECORD_MUTATION", f"{action} is unsupported")
