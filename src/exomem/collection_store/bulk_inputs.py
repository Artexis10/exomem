"""Detach bounded bulk candidates through the existing Records validators."""

from __future__ import annotations

import json
import uuid
from collections import ChainMap
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, field

from .. import origin_bindings, provenance, records
from .. import structured_collections as collections
from . import link_values, related_pages, summary, tokens, typed_storage, version_sources, views


@dataclass
class PreparedBulk:
    manifest: object
    before_hash: str
    batch_id: str
    candidates: list
    occupied: dict
    store_identity: dict
    request_hash: str
    arguments: dict
    result: dict | None = None
    plans: list | None = None
    source_inputs: list | None = None
    origins: list | None = None
    source_guards: dict | None = None
    link_notes: dict = field(default_factory=dict)
    related: related_pages.RelatedPages | None = None


def validate(rows, why, source, on_reject):
    from .writer import BULK_UPSERT_MAX_ROWS

    records._validate_why(why)
    if on_reject not in {"abort", "skip"} or not isinstance(rows, list | tuple):
        raise collections.CollectionError("INVALID_BULK_ROWS", "rows must be a list and on_reject abort or skip")
    if not rows:
        raise collections.CollectionError("BULK_UPSERT_EMPTY", "rows must not be empty")
    if len(rows) > BULK_UPSERT_MAX_ROWS:
        raise collections.CollectionError("BULK_UPSERT_TOO_MANY_ROWS",
            f"a bulk upsert takes at most {BULK_UPSERT_MAX_ROWS} rows per call; split the import "
            "into batches chained by after_container_hash",
            {"rows": len(rows), "maximum": BULK_UPSERT_MAX_ROWS,
             "batches_needed": -(-len(rows) // BULK_UPSERT_MAX_ROWS), "chain_with": "after_container_hash"})
    if source is not None and type(source) is not str:
        raise collections.CollectionError("INVALID_BULK_ROWS", "source must be a string")


def detach(writer, collection, arguments):
    """Read the candidate basis without starting publication or acquiring original bytes."""
    public = {name: arguments[name] for name in ("rows", "why", "expected_container_hash", "source", "on_reject")}
    with writer.read_snapshot(mutation=True):
        _identity, _digest, replay, row, manifest, _ = writer._identified_collection(
            "bulk_upsert", collection, public, arguments["request_id"], facade_profile="records")
        if replay is not None:
            return None, replay
        arguments = {**arguments, **deepcopy(public)}
        writer._rows_writable(manifest, "bulk_upsert")
        before = writer._container(row)
        records._expect_hash(arguments["expected_container_hash"], before, "container")
        occupied = (dict(writer.handle.release_cache.state(manifest.collection_id).occupied_path_keys)
                    if manifest.item_filename and manifest.storage.strategy != "markdown-log"
                    and manifest.view_mode != summary.SUMMARY else {})
        links = link_values.for_write(writer, manifest)
        candidates = prepare(writer, manifest, arguments["rows"], arguments["source"], arguments["_import"], links)
        identity = views.store_identity(writer.connection)
        # An import job reports itself, not each batch, so only a direct bulk upsert has a reply to list pages in.
        related = None if arguments["_import"] is not None else related_pages.for_write(
            writer, manifest, [(c.key, c.values) for c in candidates if c.error is None and c.before is None])
    return PreparedBulk(manifest, before, uuid.uuid4().hex[:12], candidates, occupied, identity,
                        _digest, arguments, link_notes=links.notes() if links is not None else {},
                        related=related), None


def acquire(root, prepared, *, why, on_reject, lifetime):
    """Finalize ordered outcomes outside SQL; failed inputs never claim a key or filename."""
    manifest = prepared.manifest
    seen, source_guards = {}, {}
    outcomes, plans, source_inputs, origins = [], [], [], []
    occupied = ChainMap({}, prepared.occupied)
    is_log = manifest.storage.strategy == "markdown-log"
    has_sources = records._bulk_sources_field(manifest)
    for candidate in prepared.candidates:
        index = candidate.index
        if candidate.error is not None:
            outcomes.append(candidate.error)
            continue
        values, body, resolved = candidate.values, candidate.body, candidate.source
        key, base, before = candidate.key, candidate.base, candidate.before
        if candidate.derived and key in seen:
            outcomes.append(records._bulk_reject(index, "DUPLICATE_ROW_KEY",
                "an earlier row in this request already holds this natural key", duplicate_of=seen[key], **base))
            continue
        if candidate.conflict:
            outcomes.append(records._bulk_reject(index, "RECORD_NATURAL_KEY_CONFLICT",
                "an existing item already holds this natural key under another identity",
                item_keys=candidate.conflict, **base))
            continue
        effective = body if body is not None else before["body"] if before else ""
        try:
            effective, _ = records._normalize_item_origin(root, manifest, key=key, values=values, body=effective,
                prior_body=before["body"] if before else None, item_path=lambda: None, render=lambda value: value,
                prepare_inputs=False, _canonical_owner=True)
            origin = origin_bindings.acquire_origin_inputs(root, effective,
                before_source=before["body"] if before else None, lifetime=lifetime)
        except (collections.CollectionError, provenance.OriginError) as error:
            if isinstance(error, provenance.OriginError):
                error = collections.CollectionError(error.code, error.reason)
            outcomes.append(records._bulk_error_row(index, error))
            continue
        if before and before["payload_hash"] == tokens.payload_hash(manifest.schema.version, key, values, effective):
            if candidate.derived:
                seen[key] = index
            outcomes.append({"index": index, **base, "outcome": "unchanged"})
            continue
        rationale = f"{why} | bulk {prepared.batch_id} {index + 1}/{len(prepared.candidates)}" + (
            "" if has_sources else f" src {resolved.path}")
        if len(rationale.encode()) > records._MAX_WHY_BYTES or "\n" in rationale:
            outcomes.append(records._bulk_reject(index, "AUDIT_RATIONALE_TOO_LONG",
                "the audit rationale for this row exceeds its bound", **base))
            continue
        if candidate.derived:
            seen[key] = index
        if before:
            path = before["view_path"]
        elif is_log:
            path = f"{manifest.storage.source}#{key}"
        elif manifest.view_mode == summary.SUMMARY:
            path = None
        else:
            path = (collections.render_item_path(manifest, values, key, occupied_path_keys=occupied)
                    if manifest.item_filename else f"{manifest.storage.source}/{key}.md")
        if path is not None:
            occupied[collections._portable_path_key(path)] = 1
        source_guards[resolved.path] = resolved.guard
        # A retracted row is the predecessor of its revival: the write names its version, never its content.
        predecessor = before or candidate.retracted
        plans.append((key, values, effective, path, predecessor, resolved.path))
        source_inputs.append(version_sources.effect_input(key,
            tokens.payload_hash(manifest.schema.version, key, values, effective), effective,
            predecessor, explicit=[resolved], prepared=origin))
        if origin is not None:
            origins.append(origin)
        outcomes.append({"index": index, **base, "outcome": "updated" if before else "inserted"})
    # nosemgrep: ep-word-search -- The bulk receipt protocol fixes these outcome names; row fields stay declared data.
    counts = {name: sum(outcome["outcome"] == name for outcome in outcomes)
              for name in ("inserted", "updated", "unchanged", "rejected")}
    prepared.result = dict(operation="bulk_upsert", collection_id=manifest.collection_id, batch_id=prepared.batch_id,
        committed=False, on_reject=on_reject, rows=outcomes, counts=counts,
        before_container_hash=prepared.before_hash, after_container_hash=prepared.before_hash)
    prepared.plans, prepared.source_inputs = plans, source_inputs
    prepared.origins, prepared.source_guards = origins, source_guards
    return prepared


@dataclass
class Candidate:
    index: int
    error: dict | None = None
    values: dict | None = None
    body: str | None = None
    source: records.ResolvedSource | None = None
    key: str | None = None
    natural_key: str | None = None
    derived: bool = False
    before: dict | None = None
    retracted: dict | None = None
    conflict: list[str] | None = None

    @property
    def base(self):
        return {"item_key": self.key, "identity": "natural-key" if self.derived else "generated",
                "source": self.source.path}


def prepare(writer, manifest, rows, source, settlement=None, links=None) -> list[Candidate]:
    """Resolve row values once, then fetch touched identities together under the caller's snapshot."""
    from .writer import _natural_key, _refuse_view_stamp

    candidates, cache = [], {}
    is_log = manifest.storage.strategy == "markdown-log"
    for index, raw in enumerate(rows):
        candidate = Candidate(index)
        candidates.append(candidate)
        if (not isinstance(raw, Mapping) or set(raw) - records._BULK_ROW_KEYS
                or not isinstance(raw.get("item"), Mapping)):
            candidate.error = records._bulk_reject(index, "INVALID_BULK_ROW",
                "a row is an object of item, optional body and source; a source is required on the row or once for the batch")
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
                candidate.error = records._bulk_reject(index, "SOURCE_REQUIRED",
                    "a row needs a preserved source reference")
                continue
            resolved = (settlement.source if settlement is not None else
                        records._resolve_bulk_source(writer.root, reference, writer._operation.allows_file, cache))
            if resolved is None:
                candidate.error = records._bulk_reject(index, "SOURCE_NOT_FOUND",
                    "source is not a preserved Sources or Evidence page", source=reference)
                continue
            values = (settlement.prepared_row(index, manifest, reference, resolved, raw["item"])
                      if settlement is not None else None)
            if values is None:
                values = records._bulk_row_values(manifest, raw["item"], resolved.path)
        except collections.CollectionError as error:
            candidate.error = records._bulk_error_row(index, error)
            continue
        derived = collections.derived_item_key(manifest, values)
        candidate.key = records._validate_item_key(derived or str(uuid.uuid4()))
        candidate.natural_key = _natural_key(manifest, values)
        candidate.values, candidate.body, candidate.source = (
            values if links is None else links.apply(values)), body, resolved
        candidate.derived = derived is not None
    _current(writer, manifest.collection_id, candidates)
    return candidates


def _current(writer, cid, candidates):
    requested = [(candidate.key, candidate.natural_key) for candidate in candidates if candidate.error is None]
    if not requested:
        return
    # Keep bounded request rows outside indexed lookups; SQLite can otherwise choose a whole-collection scan.
    # The rows come from item_rows so a retracted row under a requested key is found; it holds no natural key,
    # so the natural-key branch can only meet current rows.
    cursor = writer.connection.execute(
        "WITH requested AS (SELECT json_extract(value,'$[0]') AS item_key,"
        "json_extract(value,'$[1]') AS natural_key FROM json_each(?)), touched AS ("
        "SELECT i.row_id FROM requested r CROSS JOIN item_rows i ON i.collection_id=? AND i.item_key=r.item_key UNION "
        "SELECT i.row_id FROM requested r CROSS JOIN item_rows i ON i.collection_id=? AND i.natural_key=r.natural_key "
        "WHERE r.natural_key IS NOT NULL) "
        "SELECT i.* FROM touched t JOIN item_rows i ON i.row_id=t.row_id", (json.dumps(requested), cid, cid))
    try:
        columns = [column[0] for column in cursor.description]
        rows = [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]
    finally:
        cursor.close()
    # A retracted row has no current payload to hydrate, and its content never carries into a revival.
    gone = {row["item_key"]: row for row in rows if row["retracted_txn"] is not None}
    current = typed_storage.hydrate(writer.connection, [row for row in rows if row["retracted_txn"] is None])
    by_key = {row["item_key"]: row for row in current}
    holders = {}
    for row in current:
        if row["natural_key"] is not None:
            holders.setdefault(row["natural_key"], []).append(row["item_key"])
    for candidate in candidates:
        if candidate.error is not None:
            continue
        candidate.before = by_key.get(candidate.key)
        candidate.retracted = gone.get(candidate.key)
        candidate.conflict = sorted(holder for holder in holders.get(candidate.natural_key, ()) if holder != candidate.key)
