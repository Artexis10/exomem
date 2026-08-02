"""L6-only governance boundary shared by future Records command surfaces."""

from __future__ import annotations

import stat
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import access, record_formats, vault
from . import structured_collections as collections
from .governance import egress


@dataclass(frozen=True, slots=True)
class _RecordEnvelope:
    payload: Mapping[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return dict(self.payload)


egress.register_projector(
    "record_query",
    (
        "collection_id",
        "snapshot",
        "rows",
        "returned",
        "total_matched",
        "truncated",
        "continuation",
        "derived",
        "rendered",
        "aggregate",
        "query",
        "source_versions",
    ),
)
egress.register_projector(
    "record_manifest",
    ("collection_id", "path", "title", "storage", "templates", "plans", "views", "governance"),
)
egress.register_projector("record_template", ("collection_id", "path", "content"))
egress.register_projector(
    "record_mutation",
    (
        "operation",
        "collection_id",
        "item_key",
        "before_item_hash",
        "after_item_hash",
        "before_container_hash",
        "after_container_hash",
        "affected_paths",
        "outcome",
        "audit_correlation",
    ),
)


def full_release_filter(vault_root: Path) -> Callable[[str], bool]:
    """Return the Records full-content gate without the normal L5 walk floor."""
    root = Path(vault_root)

    def allowed(relative: str) -> bool:
        return not access.refuse_if_excluded(root, relative) and (
            egress.release_level_for_path_only(root, relative) == egress.LEVEL_FULL
        )

    return allowed


def _authorize(root: Path, relative: str, *, receipt: bool = False) -> bool:
    if access.refuse_if_excluded(root, relative):
        return False
    return (
        egress.release_level_for_path_only(
            root,
            relative,
            receipt_decision="release_authorized" if receipt else None,
        )
        == egress.LEVEL_FULL
    )


def _project_links(
    root: Path, manifest: collections.CollectionManifest, values: Mapping[str, Any]
) -> dict[str, Any]:
    """Gate only schema-declared link fields whose targets are vault artifacts."""

    def allowed_value(value: Any, spec: collections.FieldSpec) -> Any:
        if spec.type == "array" and spec.items is not None and isinstance(value, list | tuple):
            return [item for item in value if allowed_value(item, spec.items) is not None]
        if spec.type != "link" or not isinstance(value, str):
            return value
        target = value.strip().removeprefix("[[").removesuffix("]]").split("|", 1)[0]
        candidates = (target, f"{vault.kb_dirname()}/{target}")
        for candidate in candidates:
            path = root / candidate
            if path.is_file() and not _authorize(root, candidate, receipt=True):
                return None
        return value

    projected = dict(values)
    for name, spec in manifest.schema.fields.items():
        if name not in projected:
            continue
        value = allowed_value(projected[name], spec)
        if value is None or (spec.type == "array" and not value):
            projected.pop(name)
        else:
            projected[name] = value
    return projected


def resolve_collection(
    vault_root: Path, selector: str | Path | collections.CollectionManifest
) -> collections.CollectionManifest:
    """Resolve only a fully released manifest, treating every other case as absent."""
    root = Path(vault_root)
    path = selector.path if isinstance(selector, collections.CollectionManifest) else selector
    with egress.disclosure_boundary(root, "record_resolve") as collector:
        manifest = collections.resolve_collection(
            root, path, authorize_path=full_release_filter(root)
        )
        if not _authorize(root, manifest.path, receipt=True):
            raise collections.CollectionError("COLLECTION_NOT_FOUND", "collection was not found")
        egress.emit_boundary_receipt(collector)
        return manifest


def query_collection(
    vault_root: Path,
    collection: str | Path | collections.CollectionManifest,
    **kwargs: Any,
) -> record_formats.RecordQueryResult:
    """Query released Records only; authorization happens before adapter parsing."""
    root = Path(vault_root)
    with egress.disclosure_boundary(root, "record_query") as collector:
        manifest = collections.resolve_collection(
            root,
            collection.path
            if isinstance(collection, collections.CollectionManifest)
            else collection,
            authorize_path=full_release_filter(root),
        )
        if not _authorize(root, manifest.path, receipt=True) or not _authorize(
            root, manifest.storage.source, receipt=True
        ):
            raise collections.CollectionError("COLLECTION_NOT_FOUND", "collection was not found")
        result = record_formats.query_collection(
            root,
            manifest,
            authorize_path=lambda path: _authorize(root, path, receipt=True),
            project_values=lambda values: _project_links(root, manifest, values),
            **kwargs,
        )
        egress.emit_boundary_receipt(collector)
        return result


def project_query_result(result: record_formats.RecordQueryResult) -> dict[str, Any]:
    """Default-deny wire envelope for an already L6-authorized query result."""
    payload = _RecordEnvelope(
        {
            "collection_id": result.collection_id,
            "snapshot": result.snapshot,
            "rows": result.rows,
            "returned": result.returned,
            "total_matched": result.total_matched,
            "truncated": result.truncated,
            "continuation": result.continuation,
            "derived": result.derived,
            "rendered": result.rendered,
            "aggregate": result.aggregate,
            "query": dict(result.query),
            "source_versions": [
                {"path": version.path, "hash": version.hash} for version in result.source_versions
            ],
        }
    )
    return egress.project(payload, egress.LEVEL_FULL, kind="record_query") or {}


def project_mutation_receipt(receipt: Mapping[str, Any]) -> dict[str, Any]:
    """Default-deny terminal mutation receipt without arbitrary nested payloads."""
    allowed = {
        key: receipt[key]
        for key in (
            "operation",
            "collection_id",
            "item_key",
            "before_item_hash",
            "after_item_hash",
            "before_container_hash",
            "after_container_hash",
            "affected_paths",
            "outcome",
            "audit_correlation",
        )
        if key in receipt
    }
    return egress.project(_RecordEnvelope(allowed), egress.LEVEL_FULL, kind="record_mutation") or {}


def project_manifest(
    vault_root: Path, collection: str | Path | collections.CollectionManifest
) -> dict[str, Any]:
    """Project a manifest only after every returned template target is released."""
    root = Path(vault_root)
    manifest = resolve_collection(root, collection)
    allowed = full_release_filter(root)
    if not allowed(manifest.storage.source) or any(
        not allowed(template.path) for template in manifest.templates
    ):
        raise collections.CollectionError("COLLECTION_NOT_FOUND", "collection was not found")
    payload = _RecordEnvelope(
        {
            "collection_id": manifest.collection_id,
            "path": manifest.path,
            "title": manifest.title,
            "storage": {
                "strategy": manifest.storage.strategy,
                "source": manifest.storage.source,
                "format_version": manifest.storage.format_version,
            },
            "templates": [
                {"path": template.path, "default_properties": dict(template.default_properties)}
                for template in manifest.templates
            ],
            "plans": [
                {"reference": plan.reference, "query": dict(plan.query)}
                for plan in manifest.links.plans
            ],
            "views": dict(manifest.views),
            "governance": dict(manifest.governance),
        }
    )
    return egress.project(payload, egress.LEVEL_FULL, kind="record_manifest") or {}


def read_template(
    vault_root: Path,
    collection: str | Path | collections.CollectionManifest,
    template_path: str,
) -> bytes:
    """Return an explicitly declared template only after its L6 path decision."""
    root = Path(vault_root)
    with egress.disclosure_boundary(root, "record_template") as collector:
        manifest = collections.resolve_collection(
            root,
            collection.path
            if isinstance(collection, collections.CollectionManifest)
            else collection,
            authorize_path=full_release_filter(root),
        )
        declared = {template.path for template in manifest.templates}
        if (
            not _authorize(root, manifest.path, receipt=True)
            or template_path not in declared
            or not _authorize(root, template_path, receipt=True)
        ):
            raise collections.CollectionError("COLLECTION_NOT_FOUND", "collection was not found")
        try:
            data, _guard = vault.read_bounded_guarded_bytes(root, template_path, limit=512 * 1024)
        except vault.PathGuardError as error:
            raise collections.CollectionError(
                "COLLECTION_NOT_FOUND", "collection was not found"
            ) from error
        egress.emit_boundary_receipt(collector)
        return data


def precommit_authorize_mutation(
    vault_root: Path,
    manifest: collections.CollectionManifest,
    snapshot: record_formats.AdapterSnapshot | None,
    *,
    planned_paths: Iterable[str] = (),
) -> None:
    """Record a pre-publication authorization decision for the complete CAS set.

    A subset snapshot is never sufficient for a Records mutation. The receipt
    is intentionally emitted before publication and describes authorization,
    not a committed write.
    """
    root = Path(vault_root)
    paths = {manifest.path, manifest.storage.source, *planned_paths}
    require_mutation_visibility(root, manifest, planned_paths=paths)
    if snapshot is not None:
        paths.update(version.path for version in snapshot.source_versions)
        paths.update(path for path, kind, _digest in snapshot.source_inventory if kind == "file")
    with egress.disclosure_boundary(root, "record_mutation_precommit") as collector:
        for path in sorted(paths):
            if not _authorize(root, path, receipt=True):
                raise collections.CollectionError(
                    "COLLECTION_NOT_FOUND", "collection was not found"
                )
        egress.emit_boundary_receipt(collector)


def require_mutation_visibility(
    vault_root: Path,
    manifest: collections.CollectionManifest,
    *,
    planned_paths: Iterable[str] = (),
) -> None:
    """Refuse before parsing when a mutation cannot see the entire CAS set."""
    root = Path(vault_root)
    allowed = full_release_filter(root)
    if not all(allowed(path) for path in (manifest.path, manifest.storage.source, *planned_paths)):
        raise collections.CollectionError("COLLECTION_NOT_FOUND", "collection was not found")
    if manifest.storage.strategy != "markdown-items":
        return
    pending = [vault.DirectoryCensusGuard.capture(root, manifest.storage.source, max_entries=2_000)]
    candidates = 0
    while pending:
        directory = pending.pop()
        for entry in directory.entries:
            candidates += 1
            if candidates > 2_000:
                raise collections.CollectionError(
                    "RECORD_ITEM_LIMIT", "collection has too many item entries"
                )
            if not allowed(entry.relative_path):
                raise collections.CollectionError(
                    "COLLECTION_NOT_FOUND", "collection was not found"
                )
            if stat.S_ISDIR(entry.mode):
                pending.append(
                    vault.DirectoryCensusGuard.capture(root, entry.relative_path, max_entries=2_000)
                )
