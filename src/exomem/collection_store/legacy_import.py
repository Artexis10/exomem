"""Read-only legacy capture and caller-transaction staging import.

This trusted internal seam neither authorizes public access nor activates a
store. The caller owns unpublished staging state and must roll back on failure.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from .. import held_fs, planning, record_formats, records, vault
from .. import structured_collections as collections
from . import chain, governance, summary, tokens, types
from .connection import CollectionStoreError
from .legacy import LegacyAuditProof, LegacyAuditSpool

_WHY = "capture legacy state into unpublished staging"
_ENVELOPE_DOMAIN = b"exomem.collection-legacy-import-envelope:v1\0"


def _json(value) -> str:
    return records._canonical_json(value).decode("utf-8")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class LegacyProofError(CollectionStoreError):
    """A failed round-trip proof check, (a) to (f) of design §10, named as data."""

    def __init__(self, check: str, detail: str) -> None:
        super().__init__("COLLECTION_LEGACY_IMPORT_PROOF", f"check({check}): {detail}")
        self.check = check


def _refuse(check: str, detail: str):
    raise LegacyProofError(check, detail)


@dataclass(frozen=True, slots=True)
class ImportContext:
    actor: str
    imported_at: str
    import_id: str

    def __post_init__(self):
        if any(
            type(value) is not str or not value.strip() for value in (self.actor, self.import_id)
        ):
            raise ValueError("import actor and attempt ID must be supplied nonempty strings")
        if type(self.imported_at) is not str or not re.fullmatch(
            r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z", self.imported_at
        ):
            raise ValueError("import time must be supplied UTC-second time")
        dt.datetime.strptime(self.imported_at, "%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True, slots=True)
class CapturedHeldCandidate:
    candidate: records.HeldCandidate
    original_bytes: bytes
    guard: vault.PathGuard


@dataclass(frozen=True, slots=True)
class CapturedLogFrame:
    prefix: bytes
    suffix: bytes
    bom: bool
    newline: str
    final_newline: bool

    def as_json(self) -> str:
        return _json(
            {
                "version": 1,
                "text": self.prefix.decode("utf-8"),
                "suffix": self.suffix.decode("utf-8"),
                "bom": self.bom,
                "newline": self.newline,
                "final_newline": self.final_newline,
            }
        )


@dataclass(frozen=True, slots=True)
class CapturedLegacyCollection:
    root: Path
    manifest: collections.CollectionManifest
    manifest_bytes: bytes
    manifest_guard: vault.PathGuard
    snapshot: record_formats.AdapterSnapshot
    held: tuple[CapturedHeldCandidate, ...]
    held_guard: vault.DirectoryCensusGuard
    legacy_inspection: dict
    legacy_audit_paths: tuple[str, ...]
    audit_proof: LegacyAuditProof
    log_frame: CapturedLogFrame | None
    input_digest: str


@dataclass(frozen=True, slots=True)
class LegacyImportResult:
    collection_id: str
    item_count: int
    legacy_event_count: int
    checkpoint_txn_id: int
    checkpoint_transition_id: str
    checkpoint_event_hash: str


def _capture_held(root, manifest, snapshot):
    relative = records._held_directory(manifest)
    overlaps_source = not records._held_directory_is_outside_source(manifest)
    source_bytes = dict(snapshot.source_bytes) if overlaps_source else {}
    candidates = []
    names = set()
    with held_fs.acquire(root).require() as filesystem:
        result = filesystem.parent(relative)
        if not result.ok:
            if result.error.code != "MISSING":
                raise result.error
            return (), vault.DirectoryCensusGuard.capture(root, relative, max_entries=0)
        with result.require() as parent, closing(filesystem.iter_names(parent)) as iterator:
            for name in iterator:
                path = f"{relative}/{name}"
                if vault._is_registered_internal_state_artifact(path):
                    continue
                if name in names:
                    raise CollectionStoreError(
                        "COLLECTION_LEGACY_IMPORT_SOURCE_CHANGED", "held census changed"
                    )
                names.add(name)
                entry = filesystem.file(parent, name)
                if not entry.ok:
                    with filesystem.parent(path).require():
                        continue
                with entry.require():
                    if not name.endswith(".md"):
                        continue
                    held_id = collections.memory_refs.normalize_id(name[:-3])
                    if held_id is None:
                        continue
                    if overlaps_source:
                        if path not in source_bytes:
                            raise CollectionStoreError(
                                "COLLECTION_LEGACY_IMPORT_SOURCE_CHANGED", "held census changed"
                            )
                        frontmatter, _, _ = vault.parse_frontmatter(
                            record_formats._decode_item_bytes(source_bytes[path]), strict=True
                        )
                        if frontmatter.get("type") != records._HELD_TYPE:
                            continue
                        raise CollectionStoreError(
                            "HELD_DIRECTORY_UNSAFE", "held candidate overlaps canonical items"
                        )
                    if name != held_id + ".md":
                        raise CollectionStoreError(
                            "COLLECTION_LEGACY_IMPORT_HELD", "held filename is not canonical"
                        )
                    raw, guard = vault.read_bounded_guarded_bytes(
                        root, path, limit=records._MAX_HELD_BYTES
                    )
                    candidate = records._parse_held_candidate_bytes(manifest, held_id, raw)
                    candidates.append(CapturedHeldCandidate(candidate, raw, guard))
            filesystem.validate_directory(parent).require()
    census = vault.DirectoryCensusGuard.capture(root, relative, max_entries=len(names))
    if {Path(entry.relative_path).name for entry in census.entries} != names:
        raise CollectionStoreError("COLLECTION_LEGACY_IMPORT_SOURCE_CHANGED", "held census changed")
    return tuple(sorted(candidates, key=lambda entry: entry.candidate.held_id)), census


def _markers(manifest, snapshot):
    sources = dict(snapshot.source_bytes)
    for record in snapshot.records:
        marker = records._structural_audit_marker(manifest, sources[record.source.path], record)
        if marker is not None:
            yield records._AuditMarker(marker, record.source.path, record.identity.key)


def _capture_log_frame(manifest, snapshot):
    if manifest.storage.strategy != "markdown-log":
        return None
    data = dict(snapshot.source_bytes)[manifest.storage.source]
    data.decode("utf-8")  # Frame persistence must not silently replace invalid bytes.
    section = manifest.storage.descriptor["section"]
    headings = record_formats._headings_outside_fences(data)
    heading = next(
        item
        for item in headings
        if item.level == section["level"] and item.title == section["title"]
    )
    end = next(
        (
            item.start
            for item in headings
            if item.start > heading.start and item.level <= heading.level
        ),
        len(data),
    )
    start = snapshot.records[0].span.start if snapshot.records else end
    match = re.search(rb"\r\n|\n|\r", data)
    return CapturedLogFrame(
        data[:start],
        data[end:],
        data.startswith(b"\xef\xbb\xbf"),
        match.group().decode() if match else "\n",
        data.endswith((b"\n", b"\r")),
    )


def _recheck(captured):
    try:
        captured.manifest_guard.recheck(captured.root)
        for guard in captured.snapshot.path_guards + captured.snapshot.directory_guards:
            guard.recheck(captured.root)
        captured.held_guard.recheck(captured.root)
        for held in captured.held:
            held.guard.recheck(captured.root)
    except vault.PathGuardError as error:
        raise CollectionStoreError(
            "COLLECTION_LEGACY_IMPORT_SOURCE_CHANGED", "captured source changed"
        ) from error


def importable(manifest: collections.CollectionManifest) -> bool:
    """Whether the importer maps this collection's layout; a dataset keeps file authority."""
    # nosemgrep: ep-word-membership -- The store schema's `collections.layout` fixes these two layouts.
    return manifest.storage.strategy in {"markdown-items", "markdown-log"}


def capture_legacy_collection(
    root, manifest_path, *, audit: LegacyAuditSpool
) -> CapturedLegacyCollection:
    """Capture files without writes; adapters retain their existing source limits.

    Held state is materialized completely. Only audit history uses the bounded
    disk spool; this API makes no whole-migration constant-memory claim.
    """
    root = Path(root)
    relative = (
        Path(manifest_path).relative_to(root).as_posix()
        if Path(manifest_path).is_absolute()
        else str(manifest_path)
    )
    data, guard = vault.read_bounded_guarded_bytes(
        root, relative, limit=collections._MAX_MANIFEST_BYTES
    )
    manifest = collections.parse_manifest_bytes(root, root / relative, data)
    if not importable(manifest):
        raise CollectionStoreError(
            "COLLECTION_LEGACY_IMPORT_UNSUPPORTED", "dataset import is unsupported"
        )
    snapshot = record_formats.load_adapter(root, manifest).read()
    if any(record.ambiguous for record in snapshot.records):
        raise CollectionStoreError(
            "COLLECTION_LEGACY_IMPORT_DUPLICATE_ID", "ambiguous item identity"
        )
    sources = {manifest.path: data, **dict(snapshot.source_bytes)}
    if any(
        version.path not in sources or _sha(sources[version.path]) != version.hash
        for version in snapshot.source_versions
    ):
        raise CollectionStoreError(
            "COLLECTION_LEGACY_IMPORT_SOURCE_CHANGED", "captured bytes do not match source versions"
        )
    held, held_guard = _capture_held(root, manifest, snapshot)
    current_hash = (
        snapshot.source_versions[-1].hash
        if manifest.storage.strategy == "markdown-log"
        else snapshot.snapshot
    )
    captured_inspection = audit.capture_inspection(
        manifest=manifest, current_container_hash=current_hash, markers=_markers(manifest, snapshot)
    )
    inspection = captured_inspection.inspection
    proof = audit.verify(
        manifest=manifest,
        current_container_hash=current_hash,
        markers=_markers(manifest, snapshot),
        legacy_inspection=inspection,
    )
    basis = {
        "manifest": _sha(data),
        "snapshot": snapshot.snapshot,
        "sources": sorted((path, _sha(raw)) for path, raw in sources.items()),
        "held": [(entry.candidate.path, _sha(entry.original_bytes)) for entry in held],
        "held_census": [
            (entry.relative_path, entry.mode, entry.device, entry.inode)
            for entry in held_guard.entries
        ],
        "audit_inputs": proof.input_basis_digest,
        "audit_events": proof.ordered_event_digest,
        "legacy_inspection": inspection,
        "legacy_audit_paths": captured_inspection.influencing_paths,
    }
    captured = CapturedLegacyCollection(
        root,
        manifest,
        data,
        guard,
        snapshot,
        held,
        held_guard,
        inspection,
        captured_inspection.influencing_paths,
        proof,
        _capture_log_frame(manifest, snapshot),
        _sha(records._canonical_json(basis)),
    )
    _recheck(captured)
    audit.require_source_root(root)
    return captured


def _natural_key(manifest, values):
    if not manifest.schema.natural_key:
        return None
    try:
        return collections.manifest_natural_key(manifest, values)
    except ValueError:
        return None


def _body(manifest, values, body):
    return record_formats.remove_item_presentation(records._semantic_body(body, manifest, values))


def _envelope(captured, context, transition, generation, previous, *, original=None):
    checkpoint = original is None
    envelope = {
        "tag": "exomem-legacy-import",
        "version": 1,
        "phase": "checkpoint" if checkpoint else "history",
        "collection_id": captured.manifest.collection_id,
        "transition_id": transition,
        "actor": context.actor,
        "imported_at": context.imported_at,
        "import_id": context.import_id,
        "why": _WHY,
        "generation_before": generation,
        "generation_after": generation + 1,
        "manifest_version_before": None,
        "manifest_version_after": 1 if checkpoint else None,
        "prev_event_hash": previous,
        "profile_operation": json.loads(original)["operation"]
        if original is not None and captured.manifest.semantic_profile == "planning"
        else None,
        "legacy_event_sha256": None if checkpoint else _sha(original.encode("utf-8")),
        "captured_input_digest": captured.input_digest if checkpoint else None,
        "audit_input_digest": captured.audit_proof.input_basis_digest if checkpoint else None,
        "ordered_event_digest": captured.audit_proof.ordered_event_digest if checkpoint else None,
    }
    if checkpoint:
        proof = captured.audit_proof
        envelope.update(
            {
                "bounded_inspection": captured.legacy_inspection,
                "bounded_inspection_paths": list(captured.legacy_audit_paths),
                "exhaustive_assessment": {
                    "status": proof.exhaustive_status,
                    "scan_complete": proof.scan_complete,
                    "gap_count": proof.gap_count,
                    "discontinuity_count": proof.discontinuity_count,
                    "diagnostic_samples": list(proof.diagnostic_samples),
                },
                "original_history": {
                    "manifest_head": proof.manifest_head,
                    "reachable_head": proof.reachable_head,
                    "count": proof.reachable_count,
                    "ordered_event_digest": proof.ordered_event_digest,
                },
                # These claimed passes cannot commit until the independent checks succeed.
                "verification_summary": dict.fromkeys("abcdef", "passed"),
            }
        )
    return envelope


def _event_hash(envelope):
    return _sha(_ENVELOPE_DOMAIN + records._canonical_json(envelope))


def _insert_envelope(conn, txn_id, envelope, original):
    sequence, head = chain.recorded_head(conn)
    digest = _event_hash(envelope)
    try:
        conn.execute(
            "INSERT INTO txns(txn_id,transition_id,collection_id,operation,profile_operation,"
            "generation_before,generation_after,manifest_version_before,manifest_version_after,"
            "actor,why,request_id,request_hash,receipt_json,committed_at,prev_event_hash,event_hash,"
            "commit_seq,store_head_hash,legacy_event_json) VALUES(?,?,?,'legacy_import',?,?,?,?,?,?,?,NULL,NULL,?,?,?,?,?,?,?)",
            (
                txn_id,
                envelope["transition_id"],
                envelope["collection_id"],
                envelope["profile_operation"],
                envelope["generation_before"],
                envelope["generation_after"],
                envelope["manifest_version_before"],
                envelope["manifest_version_after"],
                envelope["actor"],
                envelope["why"],
                _json(envelope),
                envelope["imported_at"],
                envelope["prev_event_hash"],
                digest,
                sequence + 1,
                tokens.store_head_hash(head, sequence + 1, digest),
                original,
            ),
        )
    except sqlite3.IntegrityError as error:
        raise CollectionStoreError(
            "COLLECTION_LEGACY_IMPORT_TRANSITION_CONFLICT", "global transition identity collides"
        ) from error
    return digest


def _projection(conn, cid, path, kind, version, raw, row_id=None):
    conn.execute(
        "INSERT INTO projection_state(path,collection_id,row_id,kind,published_row_version,"
        "published_sha256,state) VALUES(?,?,?,?,?,?,'current')",
        (path, cid, row_id, kind, version, _sha(raw)),
    )


def _map_current(conn, captured, checkpoint, checkpoint_hash):
    manifest = captured.manifest
    cid = manifest.collection_id
    declared = types.type_for_manifest(manifest)
    types.register_builtins(conn, txn_id=checkpoint)
    text = captured.manifest_bytes.decode("utf-8")
    frontmatter, _, _ = vault.parse_frontmatter(text, strict=True)
    metadata = governance.manifest_metadata(text)
    audit_property = records.profile_for(manifest.semantic_profile).manifest_audit_property
    reader_version = frontmatter.get(audit_property, {}).get("version", 1)
    conn.execute(
        "INSERT INTO collections(collection_id,type_name,type_version,manifest_path,source_path,"
        "layout,manifest_version,generation,audit_head,audit_reader_version,legacy_audit_status,"
        "log_frame_json,created_txn,updated_txn) VALUES(?,?,?,?,?,?,1,?,?,?,?,?,?,?)",
        (
            cid,
            declared.name,
            declared.version,
            manifest.path,
            manifest.storage.source,
            manifest.storage.strategy,
            captured.audit_proof.reachable_count + 1,
            checkpoint_hash,
            reader_version,
            captured.legacy_inspection["status"],
            captured.log_frame.as_json() if captured.log_frame else None,
            checkpoint,
            checkpoint,
        ),
    )
    conn.execute(
        "INSERT INTO collection_manifests(collection_id,manifest_version,manifest_text,manifest_hash,"
        "schema_json,natural_key_json,txn_id,governance_json) VALUES(?,1,?,?,?,?,?,?)",
        (
            cid,
            text,
            _sha(captured.manifest_bytes),
            _json(frontmatter["item_schema"]),
            _json(list(manifest.schema.natural_key)),
            checkpoint,
            metadata,
        ),
    )
    _projection(conn, cid, manifest.path, "manifest", 1, captured.manifest_bytes)
    records_in_order = captured.snapshot.records
    if (
        manifest.storage.strategy == "markdown-log"
        and manifest.storage.descriptor["insertion"] == "newest-first"
    ):
        records_in_order = tuple(reversed(records_in_order))
    sources = dict(captured.snapshot.source_bytes)
    plans = {}
    natural_keys = set()
    for record in records_in_order:
        values = record.values
        if manifest.semantic_profile == "planning":
            planning.require_planning_profile(manifest)
            values = planning.normalize_item(
                values,
                vault_root=captured.root,
                stored=values,
                apply_defaults=False,
                validate_motivation=planning.motivation_is_governed(manifest),
            )
            planning._validate_declared_text(manifest, values)
        allowed = (records._log_note_field(manifest),) if records._log_note_field(manifest) else ()
        manifest.schema.validate(values, allowed_fields=allowed)
        values = collections.normalize_item_values(manifest.schema, values)
        key = record.identity.key
        plans[key] = values
        natural = _natural_key(manifest, values)
        if natural is not None and natural in natural_keys:
            raise CollectionStoreError(
                "COLLECTION_LEGACY_IMPORT_NATURAL_KEY_CONFLICT", "duplicate complete natural key"
            )
        natural_keys.add(natural)
        body = _body(manifest, values, record.body)
        payload = tokens.payload_hash(manifest.schema.version, key, values, body)
        path = (
            record.source.path
            if manifest.storage.strategy == "markdown-items"
            else manifest.storage.source + "#" + key
        )
        try:
            row_id = conn.execute(
                "INSERT INTO items(collection_id,item_key,natural_key,row_version,schema_version,values_json,"
                "body,payload_hash,view_path,created_txn,updated_txn,governance_json) VALUES(?,?,?,1,?,?,?,?,?,?,?,?)",
                (
                    cid,
                    key,
                    natural,
                    manifest.schema.version,
                    _json(values),
                    body,
                    payload,
                    path,
                    checkpoint,
                    checkpoint,
                    governance.row_metadata(manifest.schema, values, metadata),
                ),
            ).lastrowid
        except sqlite3.IntegrityError as error:
            raise CollectionStoreError(
                "COLLECTION_LEGACY_IMPORT_PATH_CONFLICT", "item identity or view path collides"
            ) from error
        conn.execute(
            "INSERT INTO item_versions VALUES(?,1,?,?,?,?)",
            (row_id, _json(values), body, payload, checkpoint),
        )
        if manifest.storage.strategy == "markdown-items":
            _projection(conn, cid, path, "item", 1, sources[record.source.path], row_id)
    for name in declared.validators:
        types.named_validator(name).validate(manifest, plans, write=None)
    if manifest.storage.strategy == "markdown-log":
        _projection(
            conn,
            cid,
            manifest.storage.source,
            "log",
            captured.audit_proof.reachable_count + 1,
            sources[manifest.storage.source],
        )
    for entry in captured.held:
        held = entry.candidate
        try:
            held_meta = governance.held_metadata(
                manifest.schema, held.candidate, metadata, before=plans.get(held.target_item_key)
            )
        except (ValueError, TypeError, AttributeError) as error:
            raise CollectionStoreError(
                "COLLECTION_LEGACY_IMPORT_HELD_METADATA", "held governance cannot be represented"
            ) from error
        code = next((issue["code"] for issue in held.diagnostics if issue.get("code")), None)
        conn.execute(
            "INSERT INTO held_candidates(held_id,collection_id,kind,code,candidate_json,held_bytes,"
            "diagnostics_json,view_path,created_txn,updated_at,governance_json,governance_hash) "
            "VALUES(?,?,'write-refusal',?,?,?,?,?,?,?,?,?)",
            (
                held.held_id,
                cid,
                code,
                _json(records._encode_held_value(held.candidate)),
                entry.original_bytes,
                _json(records._encode_held_value(list(held.diagnostics))),
                held.path,
                checkpoint,
                held.held_at,
                held_meta,
                _sha(entry.original_bytes),
            ),
        )
        _projection(conn, cid, held.path, "held", 1, entry.original_bytes)


def _prove_round_trip(captured, key, values, body, natural):
    manifest = captured.manifest
    if manifest.storage.strategy == "markdown-items":
        rendered = record_formats.render_markdown_item(manifest, values, key, body)
        frontmatter, parsed_body, _ = vault.parse_frontmatter(rendered, strict=True)
        profile = records.profile_for(manifest.semantic_profile)
        parsed_key = frontmatter.pop(profile.item_id_property)
        for name in ("type", "collection_id", "schema_version"):
            frontmatter.pop(name)
        parsed_values = collections.normalize_item_values(manifest.schema, frontmatter)
        parsed_body = _body(manifest, parsed_values, parsed_body)
    else:
        section = manifest.storage.descriptor["section"]
        frame = "#" * section["level"] + " " + section["title"] + "\n\n"
        rendered = frame + record_formats.render_markdown_log_item(manifest, values, key, "\n")
        parsed = (
            record_formats.MarkdownLogAdapter(captured.root, manifest)
            .read_bytes(rendered.encode())
            .records
        )
        if len(parsed) != 1:
            _refuse("b", "rendered log identity count differs")
        parsed_key, parsed_values, parsed_body = (
            parsed[0].identity.key,
            collections.normalize_item_values(manifest.schema, parsed[0].values),
            parsed[0].body,
        )
    if (parsed_key, parsed_values, parsed_body, _natural_key(manifest, parsed_values)) != (
        key,
        values,
        body,
        natural,
    ):
        _refuse("b", "render/parse values, body, identity or natural key differ")


def _prove(conn, captured, context, checkpoint, checkpoint_transition, checkpoint_hash):
    manifest = captured.manifest
    cid = manifest.collection_id
    original = {record.identity.key: record for record in captured.snapshot.records}
    stored = conn.execute(
        "SELECT item_key,values_json,body,natural_key,payload_hash,schema_version FROM items WHERE collection_id=?",
        (cid,),
    ).fetchall()
    if len(stored) != len(original) or {row[0] for row in stored} != set(original):
        _refuse("a", "row count or identity set differs")
    for key, raw_values, body, natural, payload, version in stored:
        values = json.loads(raw_values)
        record = original[key]
        recomputed = tokens.payload_hash(version, key, values, body)
        if payload != recomputed or recomputed != records._payload_hash(
            manifest, key, record.values, record.body
        ):
            _refuse("c", "payload_hash differs from captured legacy identity")
        if (
            values != collections.normalize_item_values(manifest.schema, record.values)
            or body != _body(manifest, values, record.body)
            or natural != _natural_key(manifest, values)
        ):
            _refuse("b", "stored values, body or natural key differ")
        _prove_round_trip(captured, key, values, body, natural)
    count, head, previous = 0, None, None
    checkpoint_seen = False
    ordered = hashlib.sha256(b"legacy-audit-ordered-v1\0")
    for row in conn.execute(
        "SELECT txn_id,transition_id,operation,profile_operation,generation_before,"
        "generation_after,manifest_version_before,manifest_version_after,actor,why,"
        "request_id,request_hash,receipt_json,committed_at,prev_event_hash,event_hash,legacy_event_json "
        "FROM txns WHERE collection_id=? ORDER BY txn_id",
        (cid,),
    ):
        (
            txn_id,
            transition,
            operation,
            profile_operation,
            before,
            after,
            manifest_before,
            manifest_after,
            actor,
            why,
            request_id,
            request_hash,
            receipt,
            imported_at,
            prev_hash,
            digest,
            legacy_json,
        ) = row
        expected = _envelope(captured, context, transition, count, previous, original=legacy_json)
        if checkpoint_seen:
            _refuse("d", "history follows import checkpoint")
        if legacy_json is None:
            if (txn_id, transition, digest) != (checkpoint, checkpoint_transition, checkpoint_hash):
                _refuse("d", "unexpected import checkpoint")
            checkpoint_seen = True
        if (
            (
                operation,
                profile_operation,
                before,
                after,
                manifest_before,
                manifest_after,
                actor,
                why,
                request_id,
                request_hash,
                imported_at,
                prev_hash,
            )
            != (
                "legacy_import",
                expected["profile_operation"],
                count,
                count + 1,
                None,
                1 if legacy_json is None else None,
                context.actor,
                _WHY,
                None,
                None,
                context.imported_at,
                previous,
            )
            or json.loads(receipt) != expected
            or digest != _event_hash(expected)
        ):
            _refuse("d", "import envelope or event hash differs")
        previous = digest
        if legacy_json is not None:
            event = json.loads(legacy_json)
            if event["transition_id"] != transition:
                _refuse("d", "original transition identity differs")
            raw = legacy_json.encode("utf-8")
            ordered.update(len(raw).to_bytes(8, "big"))
            ordered.update(raw)
            count += 1
            head = transition
    proof = captured.audit_proof
    if not checkpoint_seen:
        _refuse("d", "import checkpoint is missing")
    if proof.manifest_head != manifest.audit_head:
        _refuse("d", "captured manifest head differs")
    if (count, head, ordered.hexdigest()) != (
        proof.reachable_count,
        proof.reachable_head,
        proof.ordered_event_digest,
    ):
        _refuse("d", "original head, count or ordered JSON digest differs")
    row = conn.execute(
        "SELECT manifest_text,manifest_hash FROM collection_manifests WHERE collection_id=? AND manifest_version=1",
        (cid,),
    ).fetchone()
    if row is None or (row[0].encode("utf-8"), row[1]) != (
        captured.manifest_bytes,
        _sha(captured.manifest_bytes),
    ):
        _refuse("e", "manifest bytes differ")
    status = conn.execute(
        "SELECT legacy_audit_status FROM collections WHERE collection_id=?", (cid,)
    ).fetchone()
    if status is None or status[0] != captured.legacy_inspection["status"]:
        _refuse("f", "captured historical status differs")
    chain.verify_store_chain(conn)


def import_legacy_collection(
    staging_conn,
    captured: CapturedLegacyCollection,
    *,
    audit: LegacyAuditSpool,
    context: ImportContext,
) -> LegacyImportResult:
    """Map and prove within the caller's transaction; never commit or publish.

    On any failure the caller must roll back the entire staging transaction.
    A verified store chain does not certify continuous historical provenance.
    """
    if not staging_conn.in_transaction:
        raise RuntimeError("legacy import requires a caller transaction")
    if not isinstance(context, ImportContext):
        raise TypeError("import context is required")
    audit.require_source_root(captured.root)
    cid = captured.manifest.collection_id
    if staging_conn.execute("SELECT 1 FROM collections WHERE collection_id=?", (cid,)).fetchone():
        raise CollectionStoreError(
            "COLLECTION_LEGACY_IMPORT_EXISTS", "collection was already imported"
        )
    if staging_conn.execute(
        "SELECT 1 FROM collections WHERE manifest_path=? OR source_path=?",
        (captured.manifest.path, captured.manifest.storage.source),
    ).fetchone():
        raise CollectionStoreError(
            "COLLECTION_LEGACY_IMPORT_PATH_CONFLICT", "collection path is already occupied"
        )
    if captured.manifest.view_mode != "items":
        # A file collection is an items view of its rows; summary is only ever a NEW collection.
        raise summary.mode_change_refused("items", captured.manifest.view_mode)
    _recheck(captured)
    chain.verify_store_chain(staging_conn)
    first = staging_conn.execute("SELECT COALESCE(MAX(txn_id),0)+1 FROM txns").fetchone()[0]
    previous = None
    count = 0
    for event in audit.iter_reachable(captured.audit_proof):
        envelope = _envelope(
            captured, context, event.transition_id, count, previous, original=event.original_json
        )
        previous = _insert_envelope(staging_conn, first + count, envelope, event.original_json)
        count += 1
    if count != captured.audit_proof.reachable_count:
        _refuse("d", "captured reachable count differs")
    checkpoint = first + count
    checkpoint_transition = _sha(
        b"exomem.collection-legacy-import-checkpoint:v1\0"
        + records._canonical_json(
            [
                cid,
                captured.input_digest,
                captured.audit_proof.input_basis_digest,
                captured.audit_proof.ordered_event_digest,
            ]
        )
    )[:24]
    envelope = _envelope(captured, context, checkpoint_transition, count, previous)
    checkpoint_hash = _event_hash(envelope)
    _map_current(staging_conn, captured, checkpoint, checkpoint_hash)
    _insert_envelope(staging_conn, checkpoint, envelope, None)
    _prove(staging_conn, captured, context, checkpoint, checkpoint_transition, checkpoint_hash)
    _recheck(captured)
    # Iterator acquisition rechecks audit files even for an empty baseline.
    with closing(audit.iter_reachable(captured.audit_proof)) as iterator:
        next(iterator, None)
    staging_conn.execute(
        "UPDATE collections SET verified_through_txn=? WHERE collection_id=?", (checkpoint, cid)
    )
    return LegacyImportResult(
        cid,
        len(captured.snapshot.records),
        count,
        checkpoint,
        checkpoint_transition,
        checkpoint_hash,
    )
