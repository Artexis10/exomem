"""Guarded, profile-neutral structured-record mutations."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import math
import os
import re
import stat
import unicodedata
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NotRequired

from typing_extensions import TypedDict

from . import record_formats, record_governance, vault, writer_lease
from . import structured_collections as collections
from .collection_profiles import profile_for

log = logging.getLogger(__name__)

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
_MAX_AGENT_AUDIT_HISTORY = 50
_LIFECYCLE_EVENT_VERSION = 2
_LIFECYCLE_RECEIPT_VERSION = 2
_LIFECYCLE_REQUEST_DOMAIN = b"exomem-record-lifecycle-request:v2\0"
_LIFECYCLE_GAP_DOMAIN = b"exomem-record-gap:v2\0"
_LIFECYCLE_CHECKPOINT_DOMAIN = b"exomem-record-checkpoint:v2\0"
#: Identity stand-in for the pre-commit round-trip probe. The probe proves the
#: caller's values survive serialization; the real key is assigned later and is
#: never what the comparison looks at.
_ROUND_TRIP_PROBE_KEY = "00000000-0000-4000-8000-000000000000"
_MAX_RECEIVED_KEY_CHARS = 256
_HELD_TYPE = "held-record"
_HELD_DIRECTORY = "Held"
#: Tagged form for the floats strict JSON cannot spell, restored on resume.
_HELD_FLOAT_TAG = "__float__"
#: Wraps a candidate object whose shape collides with an encoder tag.
_HELD_ESCAPE_TAG = "__escaped__"
_HELD_RESERVED_SHAPES = ({_HELD_FLOAT_TAG}, {_HELD_ESCAPE_TAG})
_HELD_NON_FINITE = {
    "NaN": float("nan"),
    "Infinity": float("inf"),
    "-Infinity": float("-inf"),
}
_MAX_HELD_BYTES = 256 * 1024
_MAX_HELD_DIAGNOSTICS = 50
#: How many held files one census may open. It bounds work, never the count:
#: the count is taken from the directory listing.
_MAX_HELD_LOADS = 500
#: Refusals about the candidate's own content, and therefore the ones worth
#: preserving. A guard refusal (stale hash, natural-key conflict) is about the
#: collection's state, so re-stating the write is the whole remedy.
_CANDIDATE_CONTENT_CODES = frozenset(
    {"UNREPRESENTABLE_RECORD_VALUE", "SCHEMA_UNKNOWN_FIELD", "SCHEMA_FIELD_TYPE", "SCHEMA_ENUM"}
)
_HOLD_FAILED_WARNING = (
    "HELD_CANDIDATE_NOT_WRITTEN: the refused candidate could not be preserved; "
    "correct the named fields and resubmit it"
)
_HELD_CLEANUP_WARNING = (
    "HELD_CLEANUP_FAILED: the item committed but its held file could not be removed; "
    "discard the held reference"
)
_ITEM_KEY_REMEDIATION = (
    "item_key is the internal UUID identity of an item, not its natural key; "
    "omit item_key on append and identity derives from the declared natural key"
)


class ArtifactDeliveryProof(TypedDict):
    algorithm: str
    digest: str
    reference: str


class ArtifactDelivery(TypedDict):
    """Caller-authored field mapping for receipt-gated delivery validation."""

    evidence_page: str
    link_field: str
    reported_remote_ref: str
    reported_remote_field: str
    verified_remote_field: str
    platform_reference_field: NotRequired[str]
    platform_proof: NotRequired[ArtifactDeliveryProof]


_HOLD_FAILURES = (OSError, ValueError, TypeError, vault.BatchWriteError)


@dataclass(frozen=True, slots=True)
class _AuditMarker:
    transition_id: str
    canonical_path: str
    item_key: str


@dataclass(frozen=True, slots=True)
class _AuditEvents:
    events: tuple[dict[str, Any], ...]
    parsed: bool
    required_guards: tuple[vault.PathGuard | vault.DirectoryCensusGuard, ...] = ()


@dataclass(frozen=True, slots=True)
class _AuditChain:
    status: str
    gaps: tuple[str, ...]
    events: tuple[dict[str, Any], ...]
    complete: bool
    discontinuity: Mapping[str, Any] | None = None
    discontinuities: tuple[Mapping[str, Any], ...] = ()
    required_guards: tuple[vault.PathGuard | vault.DirectoryCensusGuard, ...] = ()


@dataclass(frozen=True, slots=True)
class _PresentationRevision:
    path: str
    text: str
    hash: str
    guard: vault.PathGuard


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), sort_keys=True, allow_nan=False
    ).encode("utf-8")


def lifecycle_request_hash(
    *,
    action: str,
    collection_id: str,
    before_manifest_hash: str,
    before_container_hash: str,
    proposed_manifest_hash: str | None,
    acknowledged_gap_codes: tuple[str, ...],
    rationale: str,
) -> str:
    """Hash the closed lifecycle request domain without user record values."""
    return hashlib.sha256(
        _LIFECYCLE_REQUEST_DOMAIN
        + _canonical_json(
            {
                "acknowledged_gap_codes": list(acknowledged_gap_codes),
                "action": action,
                "before_container_hash": before_container_hash,
                "before_manifest_hash": before_manifest_hash,
                "collection_id": collection_id,
                "proposed_manifest_hash": proposed_manifest_hash,
                "rationale": rationale,
            }
        )
    ).hexdigest()


def lifecycle_gap_fingerprint(
    *,
    prior_head: str,
    acknowledged_gap_codes: tuple[str, ...],
    before_manifest_hash: str,
    before_container_hash: str,
) -> str:
    return hashlib.sha256(
        _LIFECYCLE_GAP_DOMAIN
        + _canonical_json(
            {
                "acknowledged_gap_codes": list(acknowledged_gap_codes),
                "before_container_hash": before_container_hash,
                "before_manifest_hash": before_manifest_hash,
                "prior_head": prior_head,
            }
        )
    ).hexdigest()


def lifecycle_checkpoint_fingerprint(paths_and_hashes: tuple[tuple[str, str], ...]) -> str:
    return hashlib.sha256(
        _LIFECYCLE_CHECKPOINT_DOMAIN
        + _canonical_json([list(item) for item in sorted(paths_and_hashes)])
    ).hexdigest()


def lifecycle_guards(
    manifest: collections.CollectionManifest, snapshot: record_formats.AdapterSnapshot
) -> dict[str, str]:
    """Return the only public stale-write guards for a selected collection."""
    return {
        "expected_manifest_hash": manifest.manifest_version.hash,
        "expected_container_hash": _container_hash(manifest, snapshot),
    }


def append_record(
    vault_root: Path,
    collection: str | Path | collections.CollectionManifest,
    *,
    item: Mapping[str, Any] | None = None,
    item_key: str | None = None,
    expected_container_hash: str | None = None,
    why: str,
    body: str | None = None,
    delivery: ArtifactDelivery | None = None,
    hold: bool = True,
    held: str | None = None,
    validate_snapshot: Callable[
        [collections.CollectionManifest, record_formats.AdapterSnapshot, str, Mapping[str, Any]],
        None,
    ]
    | None = None,
) -> dict[str, Any]:
    """Append one structured item, or return a content-identical replay."""
    root = Path(vault_root)
    _validate_why(why)
    hold = _validate_hold(hold)
    held_id = _validate_held_reference(held)
    if held_id is None and item is None:
        raise collections.CollectionError("INVALID_ITEM", "item must be an object")
    overrides: Mapping[str, Any] = {} if item is None else item
    _refuse_excluded_authored_names(overrides)
    supplied_manifest = record_governance.resolve_collection_for_mutation(root, collection)
    if supplied_manifest.storage.strategy == "dataset":
        record_formats.load_adapter(root, supplied_manifest).refuse_mutation("append")
    key = (
        _validate_item_key(item_key, manifest=supplied_manifest, candidate=overrides)
        if item_key
        else None
    )
    if held_id is None:
        try:
            _validate_values(supplied_manifest, overrides)
        except collections.CollectionError as error:
            # A holdable refusal is deferred to the guarded pass, which is the
            # only place a candidate may be preserved. Nothing else changes: the
            # same refusal is raised a moment later from inside the boundary.
            if not _holding_enabled(supplied_manifest, hold) or (
                error.code not in _CANDIDATE_CONTENT_CODES
            ):
                raise
    if body is not None:
        _validate_body(body)
        if body and supplied_manifest.storage.strategy == "markdown-log":
            raise collections.CollectionError(
                "UNREPRESENTABLE_RECORD_BODY", "markdown-log storage cannot represent item bodies"
            )
    warnings: list[str] = []
    with writer_lease.active_manager().mutation_guard(root, operation="record_append"):
        manifest, manifest_text, manifest_guard = _load_guarded_manifest(root, collection)
        resumed = _load_held_candidate(root, manifest, held_id) if held_id else None
        if resumed is None:
            item = overrides
        else:
            item = _apply_held_overrides(_held_mapping(resumed.candidate, "item"), overrides)
            _refuse_excluded_authored_names(item)
        body = _resumed_body(resumed, body)
        _validate_body(body)
        if body and manifest.storage.strategy == "markdown-log":
            raise collections.CollectionError(
                "UNREPRESENTABLE_RECORD_BODY", "markdown-log storage cannot represent item bodies"
            )
        try:
            values = _validate_values(manifest, item)
        except collections.CollectionError as error:
            raise _hold_refusal(
                root,
                manifest,
                error,
                hold=hold,
                attempted_action="append",
                candidate={
                    "action": "append",
                    "item": dict(item),
                    "item_key": item_key,
                    "body": body,
                    "guards": {"expected_container_hash": expected_container_hash},
                },
                why=why,
                key_values=item,
                held_id=resumed.held_id if resumed is not None else None,
            ) from None
        if key is None:
            # An omitted identity is derived from the declared natural key, so a
            # re-stated observation replays instead of arriving as a second item.
            key = _validate_item_key(
                collections.derived_item_key(manifest, values) or str(uuid.uuid4())
            )
        record_governance.require_mutation_visibility(root, manifest)
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
        twins = _natural_key_twins(manifest, snapshot, key, values)
        if twins:
            raise collections.CollectionError(
                "RECORD_NATURAL_KEY_CONFLICT",
                "an existing item already holds this natural key under another identity",
                {"item_keys": twins},
            )
        payload_hash = _payload_hash(manifest, key, values, body)
        delivery_guard = (
            _validate_artifact_delivery(root, manifest, values, delivery)
            if delivery is not None
            else None
        )
        if validate_snapshot is not None:
            validate_snapshot(manifest, snapshot, key, values)
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
                record_governance.precommit_authorize_mutation(
                    root,
                    manifest,
                    snapshot,
                    planned_paths=(existing[0].source.path,),
                )
                if delivery_guard is not None:
                    delivery_guard.recheck(root)
                _release_held_file(root, resumed, warnings)
                return _with_warnings(
                    _result(
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
                    ),
                    warnings,
                )
            raise collections.CollectionError(
                "RECORD_ID_CONFLICT", "record ID already has different data"
            )
        audit_correlation = _transition_id()
        after_manifest_text = record_formats.render_manifest_audit_head(
            manifest_text, audit_correlation, semantic_profile=manifest.semantic_profile
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
            canonical_path, new_item_guards = _new_item_path(root, manifest, key, values, snapshot)
            directory_guards = (*directory_guards, *new_item_guards)
            replacement = record_formats.render_markdown_item(
                manifest,
                values,
                key,
                body,
                audit_correlation,
                resolve_relationship=_presentation_relationship_resolver(root, manifest, snapshot),
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
        record_governance.precommit_authorize_mutation(
            root,
            manifest,
            snapshot,
            planned_paths=tuple(write.path.relative_to(root).as_posix() for write in writes),
        )
        try:
            vault.batch_atomic_write(
                writes,
                vault_root=root,
                required_guards=(
                    *directory_guards,
                    *snapshot.path_guards,
                    *((delivery_guard,) if delivery_guard is not None else ()),
                ),
            )
        except vault.BatchWriteError:
            raise
        except (vault.PathGuardError, vault.CreateOnlyConflict, OSError, ValueError) as error:
            raise _publication_error(error) from error
        committed_path = canonical_path.relative_to(root).as_posix()
        committed = _result(
            operation="append",
            manifest=manifest,
            key=key,
            before_item_hash=None,
            after_item_hash=item_hash,
            before_container_hash=current_hash,
            after_container_hash=after_hash,
            affected_paths=[committed_path],
            payload_hash=payload_hash,
            outcome="committed",
            audit_correlation=audit_correlation,
        )
        _release_held_file(root, resumed, warnings)
        committed = _with_warnings(committed, warnings)
    # Outside the guard on purpose -- see `_due_state_carrier`.
    advisory = _due_state_carrier(
        root, after_manifest, path=committed_path, key=key, values=values
    )
    return {"due_state": advisory, **committed} if advisory else committed


def _delivery_refusal(
    code: str,
    reason: str,
    **details: Any,
) -> collections.CollectionError:
    return collections.CollectionError(code, reason, details)


def _validate_artifact_delivery(
    root: Path,
    manifest: collections.CollectionManifest,
    item: Mapping[str, Any],
    delivery: object,
) -> vault.PathGuard:
    """Validate caller-authored delivery fields against one Evidence receipt.

    This adapter sets no item value and creates no collection. It only proves
    that the values the caller supplied already describe a receipt-backed local
    Evidence artifact and an honest reported/verified remote identity state.
    """
    required = {
        "evidence_page",
        "link_field",
        "reported_remote_ref",
        "reported_remote_field",
        "verified_remote_field",
    }
    optional = {"platform_reference_field", "platform_proof"}
    if not isinstance(delivery, Mapping) or set(delivery) - required - optional:
        raise _delivery_refusal(
            "INVALID_DELIVERY",
            "delivery must use only the declared receipt-validation fields",
            allowed_fields=sorted(required | optional),
        )
    missing = sorted(required - set(delivery))
    if missing:
        raise _delivery_refusal(
            "INVALID_DELIVERY",
            f"delivery field is required: {missing[0]}",
            field=missing[0],
        )
    field_limits = {
        "evidence_page": 2048,
        "link_field": 128,
        "reported_remote_ref": 2048,
        "reported_remote_field": 128,
        "verified_remote_field": 128,
    }
    for name, limit in field_limits.items():
        value = delivery.get(name)
        if (
            type(value) is not str
            or not value.strip()
            or value != value.strip()
            or len(value.encode("utf-8")) > limit
        ):
            raise _delivery_refusal(
                "INVALID_DELIVERY", f"delivery field must be bounded non-empty text: {name}", field=name
            )

    mapped_names = [
        str(delivery["link_field"]),
        str(delivery["reported_remote_field"]),
        str(delivery["verified_remote_field"]),
    ]
    platform_field = delivery.get("platform_reference_field")
    proof = delivery.get("platform_proof")
    if (platform_field is None) != (proof is None):
        raise _delivery_refusal(
            "INVALID_DELIVERY",
            "platform_reference_field and platform_proof must be supplied together",
        )
    if platform_field is not None:
        if (
            type(platform_field) is not str
            or not platform_field.strip()
            or platform_field != platform_field.strip()
            or len(platform_field.encode("utf-8")) > 128
        ):
            raise _delivery_refusal(
                "INVALID_DELIVERY",
                "platform_reference_field must be a bounded field name",
                field="platform_reference_field",
            )
        mapped_names.append(platform_field)
    if len(set(mapped_names)) != len(mapped_names):
        raise _delivery_refusal(
            "INVALID_DELIVERY", "delivery field mappings must name distinct schema fields"
        )

    expected_types = {
        str(delivery["link_field"]): "link",
        str(delivery["reported_remote_field"]): "string",
        str(delivery["verified_remote_field"]): "boolean",
        **({str(platform_field): "string"} if platform_field is not None else {}),
    }
    for field, expected_type in expected_types.items():
        spec = manifest.schema.fields.get(field)
        if spec is None or spec.type != expected_type:
            raise _delivery_refusal(
                "DELIVERY_SCHEMA_INCOMPATIBLE",
                f"delivery field is missing or has an incompatible type: {field}",
                field=field,
                expected_type=expected_type,
                actual_type=None if spec is None else spec.type,
            )

    evidence_page = str(delivery["evidence_page"])
    try:
        page_path, page_rel = vault.resolve_under_vault(
            root, evidence_page, must_exist=True, must_be_file=True
        )
    except vault.VaultPathError as error:
        raise _delivery_refusal(
            "DELIVERY_RECEIPT_REQUIRED",
            "delivery requires an existing canonical Evidence companion receipt",
            evidence_page=evidence_page,
        ) from error
    if page_rel != evidence_page:
        raise _delivery_refusal(
            "INVALID_DELIVERY", "evidence_page must be the canonical vault-relative path"
        )
    try:
        source, page_guard = vault.read_guarded_text(root, page_path)
        frontmatter, _body, marker = vault.parse_frontmatter(source, strict=True)
    except (OSError, UnicodeDecodeError, vault.FrontmatterError, vault.PathGuardError) as error:
        raise _delivery_refusal(
            "DELIVERY_RECEIPT_REQUIRED",
            "delivery requires a readable canonical Evidence companion receipt",
            evidence_page=evidence_page,
        ) from error
    adoption = frontmatter.get("artifact_adoption") if marker is not None else None
    if not isinstance(adoption, Mapping):
        raise _delivery_refusal(
            "DELIVERY_RECEIPT_REQUIRED",
            "delivery requires a committed artifact-adoption receipt",
            evidence_page=evidence_page,
        )
    from . import client_artifacts

    try:
        receipt = client_artifacts._validate_receipt(
            adoption, page_path=page_path, vault_root=root
        )
        client_artifacts._verify_canonical_artifact(root, receipt)
    except client_artifacts.SafeFetchError as error:
        raise _delivery_refusal(
            "DELIVERY_RECEIPT_REQUIRED",
            "delivery requires a valid committed Evidence adoption receipt",
            evidence_page=evidence_page,
            receipt_error=error.code,
        ) from error
    if receipt["lane"] != "evidence":
        raise _delivery_refusal(
            "DELIVERY_EVIDENCE_REQUIRED",
            "Source adoption cannot authorize an Evidence delivery record",
            evidence_page=evidence_page,
        )

    expected_values: dict[str, Any] = {
        str(delivery["link_field"]): evidence_page,
        str(delivery["reported_remote_field"]): delivery["reported_remote_ref"],
        str(delivery["verified_remote_field"]): False,
    }
    if proof is not None:
        if not isinstance(proof, Mapping) or set(proof) != {"algorithm", "digest", "reference"}:
            raise _delivery_refusal(
                "INVALID_DELIVERY_PROOF",
                "platform_proof must contain exactly algorithm, digest, and reference",
            )
        algorithm = proof.get("algorithm")
        digest = proof.get("digest")
        reference = proof.get("reference")
        if (
            algorithm != "sha256"
            or type(digest) is not str
            or re.fullmatch(r"[0-9a-f]{64}", digest) is None
            or type(reference) is not str
            or not reference.strip()
            or reference != reference.strip()
            or len(reference.encode("utf-8")) > 2048
        ):
            raise _delivery_refusal(
                "INVALID_DELIVERY_PROOF", "platform_proof has an invalid identity shape"
            )
        if digest != receipt["hash"]:
            raise _delivery_refusal(
                "DELIVERY_PROOF_MISMATCH",
                "platform proof digest does not match the local adoption receipt",
            )
        expected_values[str(delivery["verified_remote_field"])] = True
        assert platform_field is not None
        expected_values[str(platform_field)] = reference

    for field, expected in expected_values.items():
        if field not in item or type(item[field]) is not type(expected) or item[field] != expected:
            raise _delivery_refusal(
                "DELIVERY_ITEM_MISMATCH",
                f"submitted item does not exactly match delivery evidence: {field}",
                field=field,
                expected=expected,
            )
    return page_guard


def update_record(
    vault_root: Path,
    collection: str | Path | collections.CollectionManifest,
    *,
    item_key: str,
    changes: Mapping[str, Any] | None = None,
    expected_container_hash: str,
    expected_item_version: str,
    why: str,
    operation: str = "update",
    delete_fields: tuple[str, ...] = (),
    body: str | None = None,
    refresh_presentation: bool = False,
    hold: bool = True,
    held: str | None = None,
    validate_snapshot: Callable[
        [collections.CollectionManifest, record_formats.AdapterSnapshot, str, Mapping[str, Any]],
        None,
    ]
    | None = None,
) -> dict[str, Any]:
    """Apply a guarded, exact-key update to one existing Markdown record."""
    root = Path(vault_root)
    _validate_why(why)
    hold = _validate_hold(hold)
    held_id = _validate_held_reference(held)
    if body is not None:
        _validate_body(body)
    item_key = _validate_update_item_key(root, collection, item_key, changes)
    if type(refresh_presentation) is not bool:
        raise collections.CollectionError(
            "INVALID_RECORD_PRESENTATION", "refresh_presentation must be boolean"
        )
    overrides: Mapping[str, Any] = {} if changes is None else changes
    if held_id is None and (
        not isinstance(changes, Mapping)
        or (not changes and not delete_fields and body is None and not refresh_presentation)
    ):
        raise collections.CollectionError(
            "INVALID_RECORD_CHANGES", "changes must be a non-empty object"
        )
    _refuse_excluded_authored_names(overrides)
    warnings: list[str] = []
    with writer_lease.active_manager().mutation_guard(root, operation="record_update"):
        manifest, manifest_text, manifest_guard = _load_guarded_manifest(root, collection)
        resumed = _load_held_candidate(root, manifest, held_id) if held_id else None
        if resumed is None:
            changes = overrides
        else:
            changes = _apply_held_overrides(_held_mapping(resumed.candidate, "changes"), overrides)
            _refuse_excluded_authored_names(changes)
            delete_fields = delete_fields or _held_delete_fields(resumed)
            body = _resumed_update_body(resumed, body)
        if refresh_presentation and (
            manifest.record_presentation is None and manifest.item_presentation is None
        ):
            raise collections.CollectionError(
                "INVALID_RECORD_PRESENTATION", "collection has no presentation recipe"
            )
        record_governance.require_mutation_visibility(root, manifest)
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
        final_values = dict(record.values)
        for name in delete_fields:
            final_values.pop(name, None)
        final_values.update(changes)
        if validate_snapshot is not None:
            validate_snapshot(manifest, snapshot, item_key, final_values)
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
        for name in delete_fields:
            merged.pop(name, None)
        merged.update(changes)
        try:
            values = _validate_values(manifest, merged)
        except collections.CollectionError as error:
            raise _hold_refusal(
                root,
                manifest,
                error,
                hold=hold,
                attempted_action="update",
                candidate={
                    "action": "update",
                    "item_key": item_key,
                    "changes": dict(changes),
                    "delete_fields": list(delete_fields),
                    "body": body,
                    "guards": {
                        "expected_container_hash": expected_container_hash,
                        "expected_item_version": expected_item_version,
                    },
                },
                why=why,
                key_values=merged,
                held_id=resumed.held_id if resumed is not None else None,
                target_item_key=item_key,
            ) from None
        if _natural_key_moved(manifest, record.values, values):
            twins = _natural_key_twins(manifest, snapshot, item_key, values)
            if twins:
                raise collections.CollectionError(
                    "RECORD_NATURAL_KEY_CONFLICT",
                    "an existing item already holds this natural key under another identity",
                    {"item_keys": twins},
                )
        relationship_resolver = _presentation_relationship_resolver(root, manifest, snapshot)
        if refresh_presentation and not changes and not delete_fields and body is None:
            refreshed = record_formats.splice_record_presentation(source_text, manifest, values)
            refreshed = record_formats.splice_item_presentation(
                refreshed,
                manifest,
                values,
                resolve_relationship=relationship_resolver,
            )
            if refreshed == source_text:
                raise collections.CollectionError(
                    "NOOP_RECORD_PRESENTATION", "managed presentation is already current"
                )
        audit_correlation = _transition_id()
        after_manifest_text = record_formats.render_manifest_audit_head(
            manifest_text, audit_correlation, semantic_profile=manifest.semantic_profile
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
                source_text,
                changes,
                audit_correlation,
                semantic_profile=manifest.semantic_profile,
                delete_fields=delete_fields,
                body=body,
            )
            replacement = record_formats.splice_record_presentation(replacement, manifest, values)
            replacement = record_formats.splice_item_presentation(
                replacement,
                manifest,
                values,
                resolve_relationship=relationship_resolver,
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
            operation=operation,
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
        record_governance.precommit_authorize_mutation(
            root,
            manifest,
            snapshot,
            planned_paths=(
                record.source.path,
                *(write.path.relative_to(root).as_posix() for write in log_plan.writes),
            ),
        )
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
        committed = _result(
            operation=operation,
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
        committed_path = record.source.path
        before_values = dict(record.values)
        _release_held_file(root, resumed, warnings)
        committed = _with_warnings(committed, warnings)
    # Outside the guard on purpose -- see `_due_state_carrier`.
    advisory = _due_state_carrier(
        root,
        after_manifest,
        path=committed_path,
        key=item_key,
        values=values,
        previous=before_values,
    )
    return {"due_state": advisory, **committed} if advisory else committed


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
        if path.name != "_collection.md":
            raise collections.CollectionError(
                "INVALID_COLLECTION_MANIFEST", "manifest must be _collection.md"
            )
        manifest = _preflight_collection_create(root, path, manifest_text, scaffold=scaffold)
        manifest_guard = vault.PathGuard.capture(
            root, path.relative_to(root).as_posix(), leaf_policy="absent"
        )
        _require_activity_log(root)
        audit_correlation = _transition_id()
        marked_manifest_text = record_formats.render_manifest_audit_head(
            manifest_text, audit_correlation, semantic_profile=manifest.semantic_profile
        )
        manifest = collections.parse_manifest_bytes(
            root, path, marked_manifest_text.encode("utf-8")
        )
        source = root / manifest.storage.source
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
        source_guard = None
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
        else:
            source_guard = vault.PathGuard.capture(
                root, manifest.storage.source, leaf_policy="absent"
            )
        if not scaffold or manifest.storage.strategy == "dataset":
            after_item_hash = None
            after_container_hash = _absent_source_container_hash(manifest)
        elif manifest.storage.strategy == "markdown-log":
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
        record_governance.precommit_authorize_mutation(
            root,
            manifest,
            None,
            planned_paths=(
                *affected,
                *(write.path.relative_to(root).as_posix() for write in log_plan.writes),
            ),
        )
        try:
            source_is_written = any(write.path == source for write in writes)
            vault.batch_atomic_write(
                [*writes, *log_plan.writes],
                vault_root=root,
                required_guards=(
                    *_portable_absence_guards(root, path, source),
                    *(
                        (source_guard,)
                        if source_guard is not None and not source_is_written
                        else ()
                    ),
                ),
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


def validate_collection_create(
    vault_root: Path,
    manifest_path: str | Path,
    manifest_text: str,
    *,
    scaffold: bool = True,
) -> dict[str, Any]:
    """Preflight collection creation without writer authority or vault mutation."""
    return _validate_collection_create_for_profile(
        vault_root,
        manifest_path,
        manifest_text,
        semantic_profile="records",
        scaffold=scaffold,
    )


def _validate_collection_create_for_profile(
    vault_root: Path,
    manifest_path: str | Path,
    manifest_text: str,
    *,
    semantic_profile: str,
    scaffold: bool = True,
) -> dict[str, Any]:
    """Shared create preflight with one explicit product-profile boundary."""
    root = Path(vault_root)
    if not isinstance(manifest_text, str) or not manifest_text:
        raise collections.CollectionError(
            "INVALID_COLLECTION_MANIFEST", "manifest text is required"
        )
    manifest = _preflight_collection_create(
        root, root / manifest_path, manifest_text, scaffold=scaffold
    )
    if manifest.semantic_profile != semantic_profile:
        raise collections.CollectionError(
            "RECORDS_PROFILE_REQUIRED"
            if semantic_profile == "records"
            else "PLANNING_PROFILE_REQUIRED",
            f"{semantic_profile.title()} collection is required",
        )
    _require_activity_log(root)
    if not all(
        record_governance.full_release_filter(root)(path)
        for path in (manifest.path, manifest.storage.source, f"{vault.kb_prefix()}/log.md")
    ):
        raise collections.CollectionError("COLLECTION_NOT_FOUND", "collection was not found")
    would_create = [manifest.path]
    warnings: list[dict[str, str]] = []
    if scaffold and manifest.storage.strategy in {"markdown-items", "markdown-log"}:
        would_create.append(manifest.storage.source)
    elif scaffold and manifest.storage.strategy == "dataset":
        warnings.append(
            {
                "code": "DATASET_SCAFFOLD_NOT_CREATED",
                "message": "dataset canonical sources are supplied separately",
            }
        )
    return {
        "valid": True,
        "manifest_path": manifest.path,
        "would_create": would_create,
        "normalized_contract": _normalized_manifest_contract(manifest),
        "warnings": warnings,
    }


def validate_collection_revision(
    vault_root: Path,
    collection: str | Path | collections.CollectionManifest,
    manifest_text: str,
) -> dict[str, Any]:
    """Validate a proposed existing manifest without acquiring writer authority."""
    return _validate_collection_revision_for_profile(
        vault_root,
        collection,
        manifest_text,
        semantic_profile="records",
    )


def _validate_collection_revision_for_profile(
    vault_root: Path,
    collection: str | Path | collections.CollectionManifest,
    manifest_text: str,
    *,
    semantic_profile: str,
) -> dict[str, Any]:
    """Shared revision preflight with profile-specific identity and audit names."""
    root = Path(vault_root)
    current = record_governance.resolve_collection(root, collection)
    if current.semantic_profile != semantic_profile:
        raise collections.CollectionError(
            "RECORDS_PROFILE_REQUIRED"
            if semantic_profile == "records"
            else "PLANNING_PROFILE_REQUIRED",
            f"{semantic_profile.title()} collection is required",
        )
    record_governance.require_mutation_visibility(root, current)
    snapshot = record_formats.load_adapter(root, current).read()
    _current_text, current_guard = _read_record_bytes(root, current.path)
    try:
        current_text = _current_text.decode("utf-8")
    except UnicodeDecodeError as error:
        raise collections.CollectionError(
            "INVALID_COLLECTION_MANIFEST", "manifest is not UTF-8"
        ) from error
    current_guard.recheck(root)
    proposed = _validate_revision_manifest(
        root,
        current,
        current_text,
        manifest_text,
        snapshot,
        semantic_profile=semantic_profile,
    )
    return {
        "valid": True,
        "lifecycle_guards": lifecycle_guards(current, snapshot),
        "normalized_contract": _normalized_manifest_contract(proposed),
    }


def revise_collection(
    vault_root: Path,
    collection: str | Path | collections.CollectionManifest,
    *,
    manifest_text: str,
    expected_manifest_hash: str,
    expected_container_hash: str,
    why: str,
) -> dict[str, Any]:
    """Atomically publish a guarded Records manifest revision and v2 audit event."""
    return _lifecycle_mutation(
        vault_root,
        collection,
        action="revise",
        manifest_text=manifest_text,
        expected_manifest_hash=expected_manifest_hash,
        expected_container_hash=expected_container_hash,
        acknowledged_gap_codes=(),
        why=why,
        semantic_profile="records",
    )


def rebaseline_collection(
    vault_root: Path,
    collection: str | Path | collections.CollectionManifest,
    *,
    expected_manifest_hash: str,
    expected_container_hash: str,
    acknowledged_gap_codes: tuple[str, ...] | list[str],
    why: str,
) -> dict[str, Any]:
    """Acknowledge an exact valid direct-edit gap without rewriting canonical items."""
    if not isinstance(acknowledged_gap_codes, (tuple, list)) or not all(
        isinstance(code, str) for code in acknowledged_gap_codes
    ):
        raise collections.CollectionError("INVALID_RECORD_GAPS", "gap acknowledgement is invalid")
    return _lifecycle_mutation(
        vault_root,
        collection,
        action="rebaseline",
        manifest_text=None,
        expected_manifest_hash=expected_manifest_hash,
        expected_container_hash=expected_container_hash,
        acknowledged_gap_codes=tuple(acknowledged_gap_codes),
        why=why,
        semantic_profile="records",
    )


def _lifecycle_mutation(
    vault_root: Path,
    collection: str | Path | collections.CollectionManifest,
    *,
    action: str,
    manifest_text: str | None,
    expected_manifest_hash: str,
    expected_container_hash: str,
    acknowledged_gap_codes: tuple[str, ...],
    why: str,
    semantic_profile: str,
) -> dict[str, Any]:
    root = Path(vault_root)
    _validate_why(why)
    if action not in {"revise", "rebaseline"}:
        raise collections.CollectionError("INVALID_RECORD_ACTION", "lifecycle action is invalid")
    with writer_lease.active_manager().mutation_guard(root, operation=f"record_{action}"):
        current, current_text, manifest_guard = _load_guarded_manifest(root, collection)
        if current.semantic_profile != semantic_profile:
            raise collections.CollectionError(
                "RECORDS_PROFILE_REQUIRED"
                if semantic_profile == "records"
                else "PLANNING_PROFILE_REQUIRED",
                f"{semantic_profile.title()} collection is required",
            )
        record_governance.require_mutation_visibility(root, current)
        snapshot = record_formats.load_adapter(root, current).read()
        _require_unambiguous_snapshot(snapshot)
        if current.view_diagnostics:
            diagnostic = current.view_diagnostics[0]
            raise collections.CollectionError(diagnostic.code, diagnostic.reason)
        record_formats.validate_storage_contract(current)
        record_governance.require_proposed_manifest_visibility(root, current)
        for record in snapshot.records:
            _validate_values(current, record.values)
        guards = lifecycle_guards(current, snapshot)
        _expect_hash(expected_manifest_hash, guards["expected_manifest_hash"], "manifest")
        _expect_hash(expected_container_hash, guards["expected_container_hash"], "container")
        chain = _inspect_audit_chain(
            root, current, authorize_path=record_governance.full_release_filter(root)
        )
        if action == "revise":
            if chain.status not in {"baseline", "ok", "acknowledged_gap"}:
                raise collections.CollectionError(
                    "RECORD_AUDIT_GAP", "revision requires continuous audit history"
                )
            assert manifest_text is not None
            proposed = _validate_revision_manifest(
                root,
                current,
                current_text,
                manifest_text,
                snapshot,
                semantic_profile=semantic_profile,
            )
            presentation_revisions = _plan_presentation_revision(root, current, proposed, snapshot)
            candidate_text = manifest_text
            codes: tuple[str, ...] = ()
            continuity = True
            gap_fingerprint = None
            checkpoint_fingerprint = None
        else:
            if (
                chain.status != "gap"
                or not chain.gaps
                or not _acknowledgeable_gap_codes(chain.gaps)
            ):
                raise collections.CollectionError(
                    "RECORD_AUDIT_GAP", "rebaseline requires an inspectable audit gap"
                )
            codes = _validate_gap_codes(acknowledged_gap_codes)
            if codes != chain.gaps:
                raise collections.CollectionError("STALE_RECORD", "gap acknowledgement is stale")
            proposed = current
            presentation_revisions = ()
            candidate_text = current_text
            continuity = False
            prior_head = current.audit_head or "baseline"
            gap_fingerprint = lifecycle_gap_fingerprint(
                prior_head=prior_head,
                acknowledged_gap_codes=codes,
                before_manifest_hash=guards["expected_manifest_hash"],
                before_container_hash=guards["expected_container_hash"],
            )
            checkpoint_fingerprint = lifecycle_checkpoint_fingerprint(
                tuple((version.path, version.hash) for version in snapshot.source_versions)
            )
        transition_id = _transition_id()
        marked_text = record_formats.render_manifest_audit_head(
            candidate_text,
            transition_id,
            semantic_profile=semantic_profile,
            reader_version=2,
        )
        after = collections.parse_manifest_bytes(
            root, root / current.path, marked_text.encode("utf-8")
        )
        after_container_hash = _container_hash_with_replacements(
            after,
            snapshot,
            {revision.path: revision.hash for revision in presentation_revisions},
        )
        payload_hash = lifecycle_request_hash(
            action=action,
            collection_id=current.collection_id,
            before_manifest_hash=guards["expected_manifest_hash"],
            before_container_hash=guards["expected_container_hash"],
            proposed_manifest_hash=(proposed.manifest_version.hash if action == "revise" else None),
            acknowledged_gap_codes=codes,
            rationale=why,
        )
        event = _lifecycle_audit_body(
            transition_id=transition_id,
            parent_id=current.audit_head or "baseline",
            operation=action,
            manifest=current,
            before_manifest_hash=guards["expected_manifest_hash"],
            after_manifest_hash=after.manifest_version.hash,
            before_container_hash=guards["expected_container_hash"],
            after_container_hash=after_container_hash,
            payload_hash=payload_hash,
            why=why,
            continuity=continuity,
            acknowledged_gap_codes=codes,
            gap_fingerprint=gap_fingerprint,
            checkpoint_snapshot_hash=checkpoint_fingerprint,
        )
        log_plan = _plan_required_audit(root, current, "collection", event, after_container_hash)
        # The activity plan pins the exact log bytes it will replace.  Re-read
        # the chain after planning so an out-of-band fork between the initial
        # audit decision and that guarded plan cannot be blessed by publication.
        rechecked_chain = _inspect_audit_chain(
            root, current, authorize_path=record_governance.full_release_filter(root)
        )
        if rechecked_chain.status != chain.status or rechecked_chain.gaps != chain.gaps:
            raise collections.CollectionError(
                "STALE_RECORD", "audit history changed before publication"
            )
        record_governance.precommit_authorize_mutation(
            root,
            current,
            snapshot,
            planned_paths=(
                current.path,
                *(revision.path for revision in presentation_revisions),
                *(write.path.relative_to(root).as_posix() for write in log_plan.writes),
            ),
        )
        try:
            vault.batch_atomic_write(
                [
                    *(
                        vault.PlannedWrite(
                            root / revision.path,
                            revision.text,
                            guard=revision.guard,
                        )
                        for revision in presentation_revisions
                    ),
                    vault.PlannedWrite(root / current.path, marked_text, guard=manifest_guard),
                    *log_plan.writes,
                ],
                vault_root=root,
                required_guards=(
                    *(
                        guard
                        for guard in snapshot.path_guards
                        if guard.target
                        not in {revision.path for revision in presentation_revisions}
                    ),
                    *snapshot.directory_guards,
                    *rechecked_chain.required_guards,
                ),
            )
        except vault.BatchWriteError:
            raise
        except (vault.PathGuardError, vault.CreateOnlyConflict, OSError, ValueError) as error:
            raise _publication_error(error) from error
        return _lifecycle_result(
            operation=action,
            manifest=current,
            before_manifest_hash=guards["expected_manifest_hash"],
            after_manifest_hash=after.manifest_version.hash,
            before_container_hash=guards["expected_container_hash"],
            after_container_hash=after_container_hash,
            payload_hash=payload_hash,
            audit_correlation=transition_id,
            continuity=continuity,
            acknowledged_gap_codes=codes,
            gap_fingerprint=gap_fingerprint,
            checkpoint_snapshot_hash=checkpoint_fingerprint,
            affected_paths=(
                current.path,
                *(revision.path for revision in presentation_revisions),
            ),
        )


def _validate_revision_manifest(
    root: Path,
    current: collections.CollectionManifest,
    current_text: str,
    manifest_text: str,
    snapshot: record_formats.AdapterSnapshot,
    *,
    semantic_profile: str,
) -> collections.CollectionManifest:
    if not isinstance(manifest_text, str) or not manifest_text:
        raise collections.CollectionError(
            "INVALID_COLLECTION_MANIFEST", "manifest text is required"
        )
    proposed = collections.parse_manifest_bytes(
        root, root / current.path, manifest_text.encode("utf-8")
    )
    _refuse_excluded_manifest_fields(proposed)
    if proposed.view_diagnostics:
        diagnostic = proposed.view_diagnostics[0]
        raise collections.CollectionError(diagnostic.code, diagnostic.reason)
    if proposed.semantic_profile != semantic_profile or (
        proposed.collection_id != current.collection_id
        or proposed.semantic_profile != current.semantic_profile
        or proposed.storage.strategy != current.storage.strategy
        or proposed.storage.source != current.storage.source
        or proposed.storage.format_version != current.storage.format_version
        or dict(proposed.storage.descriptor) != dict(current.storage.descriptor)
        or proposed.schema.version != current.schema.version
        or proposed.schema.natural_key != current.schema.natural_key
    ):
        raise collections.CollectionError(
            "IMMUTABLE_COLLECTION_REPRESENTATION",
            "revision cannot migrate collection representation",
        )
    proposed_audit = _manifest_audit_mapping(manifest_text, semantic_profile)
    current_audit = _manifest_audit_mapping(current_text, semantic_profile)
    if proposed_audit is not None and proposed_audit != current_audit:
        raise collections.CollectionError(
            "INVALID_RECORD_AUDIT", "proposed audit state must match current state"
        )
    record_formats.validate_storage_contract(proposed)
    record_governance.require_proposed_manifest_visibility(root, proposed)
    for record in snapshot.records:
        _validate_values(proposed, record.values)
        if proposed.record_presentation is not None:
            record_formats._presentation_payload(proposed, record.values)
        if proposed.item_presentation is not None:
            record_formats._item_presentation_digests(proposed, record.values)
    _plan_presentation_revision(root, current, proposed, snapshot)
    return proposed


def _plan_presentation_revision(
    root: Path,
    current: collections.CollectionManifest,
    proposed: collections.CollectionManifest,
    snapshot: record_formats.AdapterSnapshot,
) -> tuple[_PresentationRevision, ...]:
    """Plan exact owned-block cleanup or conversion for one manifest revision."""
    presentation_changed = (
        current.record_presentation != proposed.record_presentation
        or current.item_presentation != proposed.item_presentation
    )
    if not presentation_changed:
        return ()
    if current.storage.strategy != "markdown-items":
        raise collections.CollectionError(
            "IMMUTABLE_COLLECTION_REPRESENTATION",
            "presentation revision requires markdown-items storage",
        )
    current_owner = (
        "legacy"
        if current.record_presentation is not None
        else "shared"
        if current.item_presentation is not None
        else None
    )
    proposed_owner = (
        "legacy"
        if proposed.record_presentation is not None
        else "shared"
        if proposed.item_presentation is not None
        else None
    )
    source_bytes = dict(snapshot.source_bytes)
    guards = {
        guard.target: guard for guard in snapshot.path_guards if isinstance(guard, vault.PathGuard)
    }
    resolve_relationship = record_formats.presentation_relationship_resolver(
        root,
        proposed,
        snapshot,
        authorize_path=record_governance.full_release_filter(root),
    )
    revisions: list[_PresentationRevision] = []
    for record in snapshot.records:
        data = source_bytes.get(record.source.path)
        guard = guards.get(record.source.path)
        if data is None or guard is None:
            raise collections.CollectionError(
                "SOURCE_NOT_FOUND", "item presentation source is unavailable"
            )
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as error:
            raise collections.CollectionError(
                "INVALID_RECORD_ITEM", "record item is not UTF-8"
            ) from error
        legacy_present = bool(
            record_formats._PRESENTATION_OPEN.search(text)
            or record_formats._PRESENTATION_CLOSE.search(text)
        )
        shared_present = bool(
            record_formats._ITEM_PRESENTATION_OPEN.search(text)
            or record_formats._ITEM_PRESENTATION_CLOSE.search(text)
        )
        if legacy_present and shared_present:
            raise collections.CollectionError(
                "ORPHANED_ITEM_PRESENTATION",
                "item contains multiple managed presentation owners",
            )
        try:
            legacy_span = record_formats._presentation_span(text) if legacy_present else None
            shared_span = record_formats._item_presentation_span(text) if shared_present else None
        except collections.CollectionError as error:
            raise collections.CollectionError(
                "ORPHANED_ITEM_PRESENTATION",
                "managed presentation markers must be repaired before revision",
            ) from error

        if current_owner is None:
            if (legacy_present and proposed_owner != "legacy") or (
                shared_present and proposed_owner != "shared"
            ):
                raise collections.CollectionError(
                    "ORPHANED_ITEM_PRESENTATION",
                    "revision would retain an unowned managed presentation",
                )
            continue

        owned_span = legacy_span if current_owner == "legacy" else shared_span
        other_present = shared_present if current_owner == "legacy" else legacy_present
        if other_present:
            raise collections.CollectionError(
                "ORPHANED_ITEM_PRESENTATION",
                "revision would retain an unowned managed presentation",
            )
        if owned_span is None:
            continue
        updated = (
            record_formats.remove_record_presentation(text)
            if current_owner == "legacy"
            else record_formats.remove_item_presentation(text)
        )
        if proposed_owner == "legacy":
            updated = record_formats.splice_record_presentation(updated, proposed, record.values)
        elif proposed_owner == "shared":
            updated = record_formats.splice_item_presentation(
                updated,
                proposed,
                record.values,
                resolve_relationship=resolve_relationship,
            )
        if updated != text:
            revisions.append(
                _PresentationRevision(
                    path=record.source.path,
                    text=updated,
                    hash=hashlib.sha256(updated.encode("utf-8")).hexdigest(),
                    guard=guard,
                )
            )
    return tuple(revisions)


def _manifest_audit_mapping(text: str, semantic_profile: str) -> dict[str, Any] | None:
    try:
        frontmatter, _body, marker = vault.parse_frontmatter(text, strict=True)
    except vault.FrontmatterError as error:
        raise collections.CollectionError(
            "INVALID_COLLECTION_MANIFEST", "manifest requires valid frontmatter"
        ) from error
    if marker is None:
        raise collections.CollectionError(
            "INVALID_COLLECTION_MANIFEST", "manifest requires frontmatter"
        )
    audit = frontmatter.get(profile_for(semantic_profile).manifest_audit_property)
    return dict(audit) if isinstance(audit, Mapping) else None


def _validate_gap_codes(value: tuple[str, ...]) -> tuple[str, ...]:
    codes = tuple(sorted(set(value)))
    if (
        not codes
        or len(codes) != len(value)
        or any(not code or len(code.encode("utf-8")) > 256 for code in codes)
    ):
        raise collections.CollectionError("INVALID_RECORD_GAPS", "gap acknowledgement is invalid")
    return codes


def _acknowledgeable_gap_codes(codes: tuple[str, ...]) -> bool:
    return set(codes) <= {"current-container-mismatch", "current-manifest-mismatch"}


def _require_unambiguous_snapshot(snapshot: record_formats.AdapterSnapshot) -> None:
    if snapshot.diagnostics or any(record.ambiguous for record in snapshot.records):
        raise collections.CollectionError(
            "INVALID_RECORD_COLLECTION", "collection items are not structurally valid"
        )


def _container_hash(
    manifest: collections.CollectionManifest, snapshot: record_formats.AdapterSnapshot
) -> str:
    return _container_hash_with_replacements(manifest, snapshot, {})


def _container_hash_with_replacements(
    manifest: collections.CollectionManifest,
    snapshot: record_formats.AdapterSnapshot,
    replacements: Mapping[str, str],
) -> str:
    if manifest.storage.strategy == "markdown-log":
        return snapshot.source_versions[-1].hash
    pairs = [(manifest.manifest_version.path, manifest.manifest_version.hash)] + [
        (f"{kind}:{path}", replacements.get(path, digest))
        for path, kind, digest in snapshot.source_inventory
    ]
    return hashlib.sha256(_canonical_json(pairs)).hexdigest()


def _lifecycle_audit_body(
    *,
    transition_id: str,
    parent_id: str,
    operation: str,
    manifest: collections.CollectionManifest,
    before_manifest_hash: str,
    after_manifest_hash: str,
    before_container_hash: str,
    after_container_hash: str,
    payload_hash: str,
    why: str,
    continuity: bool,
    acknowledged_gap_codes: tuple[str, ...],
    gap_fingerprint: str | None,
    checkpoint_snapshot_hash: str | None,
) -> str:
    profile = profile_for(manifest.semantic_profile)
    public_operation = f"plan_{operation}" if profile.name == "planning" else operation
    event = {
        "version": _LIFECYCLE_EVENT_VERSION,
        "transition_id": transition_id,
        "parent_id": parent_id,
        "operation": public_operation,
        "collection_id": manifest.collection_id,
        "manifest_path": manifest.path,
        "source_path": manifest.storage.source,
        "canonical_path": manifest.path,
        "item_key": None,
        "before_manifest_hash": before_manifest_hash,
        "after_manifest_hash": after_manifest_hash,
        "before_item_hash": None,
        "after_item_hash": None,
        "before_container_hash": before_container_hash,
        "after_container_hash": after_container_hash,
        "payload_hash": payload_hash,
        "rationale": why,
        "continuity": continuity,
        "acknowledged_gap_codes": list(acknowledged_gap_codes),
        "gap_fingerprint": gap_fingerprint,
        "checkpoint_snapshot_hash": checkpoint_snapshot_hash,
        "minimum_reader_version": 2,
    }
    return profile.activity_prefix + json.dumps(
        event, ensure_ascii=False, separators=(",", ":"), allow_nan=False, sort_keys=True
    )


def _lifecycle_result(
    *,
    operation: str,
    manifest: collections.CollectionManifest,
    before_manifest_hash: str,
    after_manifest_hash: str,
    before_container_hash: str,
    after_container_hash: str,
    payload_hash: str,
    audit_correlation: str,
    continuity: bool,
    acknowledged_gap_codes: tuple[str, ...],
    gap_fingerprint: str | None,
    checkpoint_snapshot_hash: str | None,
    affected_paths: tuple[str, ...],
) -> dict[str, Any]:
    profile = profile_for(manifest.semantic_profile)
    return {
        "_record_receipt" if profile.name == "records" else "_plan_receipt": (
            _RECEIPT_MARKER if profile.name == "records" else "exomem.planning-mutation"
        ),
        "receipt_version": _LIFECYCLE_RECEIPT_VERSION,
        "operation": operation,
        "collection_id": manifest.collection_id,
        "item_key": None,
        "before_item_hash": None,
        "after_item_hash": None,
        "before_manifest_hash": before_manifest_hash,
        "after_manifest_hash": after_manifest_hash,
        "before_container_hash": before_container_hash,
        "after_container_hash": after_container_hash,
        "affected_paths": list(affected_paths),
        "payload_hash": payload_hash,
        "outcome": "committed",
        "audit_correlation": audit_correlation,
        "continuity": continuity,
        "acknowledged_gap_codes": list(acknowledged_gap_codes),
        "gap_fingerprint": gap_fingerprint,
        "checkpoint_snapshot_hash": checkpoint_snapshot_hash,
        "minimum_reader_version": 2,
    }


def _preflight_collection_create(
    root: Path,
    path: Path,
    manifest_text: str,
    *,
    scaffold: bool,
) -> collections.CollectionManifest:
    record_governance.require_candidate_manifest_visibility(root, path.relative_to(root).as_posix())
    _assert_portable_absent(root, path)
    if path.exists() or _casefold_alias(path):
        raise collections.CollectionError(
            "CREATE_ONLY_CONFLICT", "collection manifest already exists"
        )
    manifest = collections.parse_manifest_bytes(root, path, manifest_text.encode("utf-8"))
    _refuse_excluded_manifest_fields(manifest)
    if manifest.view_diagnostics:
        diagnostic = manifest.view_diagnostics[0]
        raise collections.CollectionError(diagnostic.code, diagnostic.reason)
    record_formats.validate_storage_contract(manifest)
    source = root / manifest.storage.source
    _assert_portable_absent(root, source)
    if source.exists() or _casefold_alias(source):
        raise collections.CollectionError("CREATE_ONLY_CONFLICT", "canonical source already exists")
    if scaffold and manifest.storage.strategy == "markdown-log":
        # Descriptor shape is validated above; this access pins the exact scaffold fields.
        manifest.storage.descriptor["section"]["title"]
        manifest.storage.descriptor["section"]["level"]
    return manifest


def _normalized_manifest_contract(
    manifest: collections.CollectionManifest,
) -> dict[str, Any]:
    return {
        "collection_id": manifest.collection_id,
        "title": manifest.title,
        "semantic_profile": manifest.semantic_profile,
        "collection_version": manifest.collection_version,
        "schema_version": manifest.schema.version,
        "lifecycle": manifest.lifecycle,
        "storage": {
            "strategy": manifest.storage.strategy,
            "source": manifest.storage.source,
            "format_version": manifest.storage.format_version,
        },
        "natural_key": list(manifest.schema.natural_key),
        "fields": {
            name: {
                "type": spec.type,
                "required": spec.required,
                **({"enum": list(spec.enum)} if spec.enum else {}),
                **({"units": list(spec.units)} if spec.units else {}),
                **({"link_kind": spec.link_kind} if spec.link_kind is not None else {}),
            }
            for name, spec in manifest.schema.fields.items()
        },
        "views": {
            name: dict(collections.resolve_saved_view(manifest, name).definition)
            for name in manifest.views
        },
    }


def inspect_audit_gap(
    vault_root: Path,
    collection: str | Path | collections.CollectionManifest,
    *,
    authorize_path: Callable[[str], bool] | None = None,
) -> dict[str, Any]:
    """Report, without repair, whether current bytes prove an audit transition chain."""
    chain = _inspect_audit_chain(Path(vault_root), collection, authorize_path=authorize_path)
    payload: dict[str, Any] = {"status": chain.status, "gaps": list(chain.gaps)}
    if chain.discontinuity is not None:
        payload["discontinuity"] = dict(chain.discontinuity)
    if chain.discontinuities:
        payload["discontinuities"] = [dict(item) for item in chain.discontinuities]
    return payload


def agent_audit_history(
    vault_root: Path,
    collection: str | Path | collections.CollectionManifest,
    *,
    authorize_path: Callable[[str], bool] | None = None,
) -> dict[str, Any]:
    """Return the bounded agent-mutation chain without canonical item values."""
    chain = _inspect_audit_chain(Path(vault_root), collection, authorize_path=authorize_path)
    events = chain.events[:_MAX_AGENT_AUDIT_HISTORY]
    return {
        "status": chain.status,
        "complete": chain.complete,
        "truncated": len(chain.events) > len(events),
        "events": [_project_agent_audit_event(event) for event in events],
        **({"discontinuity": dict(chain.discontinuity)} if chain.discontinuity is not None else {}),
        **(
            {"discontinuities": [dict(item) for item in chain.discontinuities]}
            if chain.discontinuities
            else {}
        ),
    }


def _inspect_audit_chain(
    root: Path,
    collection: str | Path | collections.CollectionManifest,
    *,
    authorize_path: Callable[[str], bool] | None,
) -> _AuditChain:
    denied = False

    def authorize(relative: str) -> bool:
        nonlocal denied
        if authorize_path is None:
            return True
        if authorize_path(relative):
            return True
        denied = True
        return False

    try:
        history_guard = vault.DirectoryCensusGuard.capture(
            root, f"{vault.kb_prefix()}_archive/logs", max_entries=128
        )
    except vault.PathGuardError:
        return _incomplete_audit_chain()
    try:
        manifest = (
            collection
            if isinstance(collection, collections.CollectionManifest)
            else collections.resolve_collection(root, collection, authorize_path=authorize)
        )
        if not authorize(manifest.path):
            return _incomplete_audit_chain()
    except collections.CollectionError:
        return _incomplete_audit_chain()
    manifest_bytes = _safe_audit_read(
        root, manifest.path, manifest.manifest_version.hash, _MAX_AUDIT_SOURCE_BYTES
    )
    if manifest_bytes is None:
        return _incomplete_audit_chain()
    try:
        head = collections.parse_manifest_bytes(
            root, root / manifest.path, manifest_bytes
        ).audit_head
    except (UnicodeDecodeError, collections.CollectionError):
        return _incomplete_audit_chain()
    if not authorize(manifest.storage.source):
        return _incomplete_audit_chain()
    try:
        snapshot = record_formats.load_adapter(root, manifest, authorize_path=authorize).read()
    except collections.CollectionError:
        if denied:
            return _incomplete_audit_chain()
        try:
            vault.PathGuard.capture(root, manifest.storage.source, leaf_policy="absent")
        except vault.PathGuardError:
            return (
                _audit_chain("gap", ("canonical-source-unavailable",), (), complete=False)
                if head is not None
                else _incomplete_audit_chain()
            )
        snapshot = None
        current_hash = _absent_source_container_hash(manifest)
        markers: tuple[_AuditMarker, ...] = ()
    else:
        assert snapshot is not None
        # A markdown-items adapter deliberately omits denied files so ordinary
        # queries can be reduced over the released subset. Audit verification
        # is different: a subset hash can never prove the whole container.
        if denied:
            return _incomplete_audit_chain()
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
            return _incomplete_audit_chain()
        snapshot_markers = _audit_markers(root, manifest, snapshot)
        if snapshot_markers is None:
            return _incomplete_audit_chain()
        markers = snapshot_markers
    return _reconstruct_audit_chain(
        root,
        manifest,
        head=head,
        manifest_hash=manifest.manifest_version.hash,
        current_hash=current_hash,
        markers=markers,
        history_guard=history_guard,
        authorize_path=authorize_path,
    )


def _incomplete_audit_chain() -> _AuditChain:
    return _audit_chain("history_incomplete", (), (), complete=False)


def _audit_chain(
    status: str,
    gaps: tuple[str, ...],
    events: tuple[dict[str, Any], ...],
    *,
    complete: bool,
    discontinuity: Mapping[str, Any] | None = None,
    discontinuities: tuple[Mapping[str, Any], ...] = (),
    required_guards: tuple[vault.PathGuard | vault.DirectoryCensusGuard, ...] = (),
) -> _AuditChain:
    return _AuditChain(
        status, gaps, events, complete, discontinuity, discontinuities, required_guards
    )


def _reconstruct_audit_chain(
    root: Path,
    manifest: collections.CollectionManifest,
    *,
    head: str | None,
    manifest_hash: str,
    current_hash: str,
    markers: tuple[_AuditMarker, ...],
    history_guard: vault.DirectoryCensusGuard | None = None,
    authorize_path: Callable[[str], bool] | None = None,
) -> _AuditChain:
    history = _audit_events(
        root,
        semantic_profile=manifest.semantic_profile,
        history_guard=history_guard,
        authorize_path=authorize_path,
    )
    events = history.events
    relevant = [event for event in events if event["collection_id"] == manifest.collection_id]
    influencing = _audit_influencing_events(
        events, relevant, collection_id=manifest.collection_id, head=head
    )
    if authorize_path is not None and any(
        not authorize_path(path)
        for event in influencing
        for path in (event["manifest_path"], event["source_path"], event["canonical_path"])
    ):
        # Do not disclose a partial event chain or even audit-gap topology when
        # a current/deleted item is outside this request's release boundary.
        return _incomplete_audit_chain()
    if head is None and not markers and not relevant:
        return _audit_chain(
            "baseline" if history.parsed else "history_incomplete", (), (), complete=history.parsed
        )
    if not history.parsed:
        return _incomplete_audit_chain()
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
    discontinuities: list[Mapping[str, Any]] = []
    if head is None:
        gaps.append("missing-manifest-head")
    current = by_id.get(head or "")
    chain: list[dict[str, Any]] = []
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
            chain.append(cursor)
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
            if cursor.get("operation") in {"rebaseline", "plan_rebaseline"} and cursor.get(
                "continuity"
            ) is False:
                discontinuities.append(
                    {
                    "provenance_continuity": False,
                    "prior_head": cursor["parent_id"],
                    "acknowledged_gap_codes": list(cursor["acknowledged_gap_codes"]),
                    "rationale": cursor["rationale"],
                    "checkpoint_transition": cursor["transition_id"],
                    "gap_fingerprint": cursor["gap_fingerprint"],
                    "checkpoint_snapshot_hash": cursor["checkpoint_snapshot_hash"],
                    }
                )
            elif (
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
    return _audit_chain(
        "gap" if gaps else ("acknowledged_gap" if discontinuities else "ok"),
        tuple(sorted(set(gaps))[:32]),
        tuple(chain),
        complete=not gaps,
        discontinuity=discontinuities[0] if discontinuities else None,
        discontinuities=tuple(discontinuities[:16]),
        required_guards=history.required_guards,
    )


def _audit_influencing_events(
    events: tuple[dict[str, Any], ...],
    relevant: list[dict[str, Any]],
    *,
    collection_id: str,
    head: str | None,
) -> tuple[dict[str, Any], ...]:
    """Return only foreign events that can alter this collection's audit result."""
    relevant_ids = {event["transition_id"] for event in relevant}
    if head is not None:
        relevant_ids.add(head)
    matching_by_id: dict[str, dict[str, Any]] = {}
    for event in relevant:
        matching_by_id.setdefault(event["transition_id"], event)
    reachable: set[str] = set()
    cursor = matching_by_id.get(head or "")
    depth = 0
    while cursor is not None and cursor["transition_id"] not in reachable and depth <= 2048:
        transition = cursor["transition_id"]
        reachable.add(transition)
        parent = cursor["parent_id"]
        if parent in {"baseline", "absent"}:
            break
        cursor = matching_by_id.get(parent)
        depth += 1
    influencing: list[dict[str, Any]] = []
    for event in events:
        if (
            event["collection_id"] == collection_id
            or event["transition_id"] in relevant_ids
            or event["parent_id"] in reachable
        ):
            influencing.append(event)
    return tuple(influencing)


def _project_agent_audit_event(event: Mapping[str, Any]) -> dict[str, Any]:
    names: tuple[str, ...] = (
            "transition_id",
            "parent_id",
            "operation",
            "item_key",
            "canonical_path",
            "before_manifest_hash",
            "after_manifest_hash",
            "before_item_hash",
            "after_item_hash",
            "before_container_hash",
            "after_container_hash",
            "rationale",
    )
    if event.get("version") == _LIFECYCLE_EVENT_VERSION:
        names += (
            "continuity",
            "acknowledged_gap_codes",
            "gap_fingerprint",
            "checkpoint_snapshot_hash",
            "minimum_reader_version",
        )
    return {name: event[name] for name in names}


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
    if not _valid_audit_event(event, manifest.semantic_profile) or not _event_matches_collection(
        event, manifest
    ):
        return False
    source = manifest.storage.source
    operation = event["operation"]
    item_key = event["item_key"]
    profile = profile_for(manifest.semantic_profile)
    create_operation = "create" if profile.name == "records" else "plan_create"
    if operation == create_operation:
        return (
            event["parent_id"] == "absent"
            and item_key is None
            and event["canonical_path"] == source
            and event["before_manifest_hash"] is None
            and event["before_item_hash"] is None
            and event["before_container_hash"] is None
        )
    if operation in {"revise", "rebaseline", "plan_revise", "plan_rebaseline"}:
        return (
            item_key is None
            and event["canonical_path"] == manifest.path
            and event["before_item_hash"] is None
            and event["after_item_hash"] is None
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
    marker = re.compile(
        rb"#\s*"
        + re.escape(profile_for(manifest.semantic_profile).item_audit_marker.encode())
        + rb":\s*([0-9a-f]{24})\s*"
    )
    audit = marker.fullmatch(lines[-1].strip())
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
    markers = _audit_markers(root, manifest, snapshot)
    if markers is None:
        return None
    current_hash = (
        snapshot.source_versions[-1].hash
        if manifest.storage.strategy == "markdown-log"
        else snapshot.snapshot
    )
    chain = _reconstruct_audit_chain(
        root,
        manifest,
        head=manifest.audit_head,
        manifest_hash=manifest.manifest_version.hash,
        current_hash=current_hash,
        markers=markers,
    )
    if chain.status != "ok":
        return None
    matching = [
        event
        for event in chain.events
        if event["transition_id"] == correlation
        and _event_matches_transition(event, manifest)
        and event["operation"]
        == ("append" if manifest.semantic_profile == "records" else "plan_add")
        and event["item_key"] == record.identity.key
        and event["canonical_path"] == record.source.path
        and event["after_item_hash"] == record.source.hash
        and event["payload_hash"] == payload_hash
    ]
    return correlation if len(matching) == 1 else None


def _audit_events(
    root: Path,
    *,
    semantic_profile: str = "records",
    history_guard: vault.DirectoryCensusGuard | None = None,
    authorize_path: Callable[[str], bool] | None = None,
) -> _AuditEvents:
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
                return _AuditEvents((), False)
            candidates.extend(
                entry.relative_path
                for entry in entries
                if re.fullmatch(r"log-[0-9a-f]{20}\.md", Path(entry.relative_path).name)
            )
    except vault.PathGuardError:
        return _AuditEvents((), False)
    events: list[dict[str, Any]] = []
    file_guards: list[vault.PathGuard] = []
    total = 0
    for relative in candidates:
        if authorize_path is not None and not authorize_path(relative):
            return _AuditEvents((), False)
        try:
            data, file_guard = vault.read_bounded_guarded_bytes(root, relative, limit=2_000_000)
            text = data.decode("utf-8")
        except (vault.PathGuardError, UnicodeDecodeError):
            return _AuditEvents((), False)
        total += len(data)
        file_guards.append(file_guard)
        if total > 8_000_000:
            return _AuditEvents((), False)
        prefix = profile_for(semantic_profile).activity_prefix
        for line in text.splitlines():
            if not line.startswith(prefix):
                continue
            try:
                event = json.loads(line.removeprefix(prefix))
            except json.JSONDecodeError:
                return _AuditEvents((), False)
            if not _valid_audit_event(event, semantic_profile):
                return _AuditEvents((), False)
            events.append(event)
            if len(events) > 10_000:
                return _AuditEvents((), False)
    try:
        if archive_guard is not None:
            archive_guard.recheck(root)
        for file_guard in file_guards:
            file_guard.recheck(root)
    except vault.PathGuardError:
        return _AuditEvents((), False)
    return _AuditEvents(
        tuple(events),
        True,
        tuple(
            (
                *((archive_guard,) if archive_guard is not None else ()),
                *(guard for guard in file_guards if guard.target != live),
            )
        ),
    )


def _valid_audit_event(event: Any, semantic_profile: str = "records") -> bool:
    if isinstance(event, dict) and event.get("version") == _LIFECYCLE_EVENT_VERSION:
        return _valid_lifecycle_audit_event(event, semantic_profile)
    if not _audit_event_syntax(event, semantic_profile):
        return False
    profile = profile_for(semantic_profile)
    create_operation = "create" if profile.name == "records" else "plan_create"
    add_operation = "append" if profile.name == "records" else "plan_add"
    if event["operation"] == create_operation:
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
    if event["operation"] == add_operation:
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


def _audit_event_syntax(event: Any, semantic_profile: str = "records") -> bool:
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
        and event["operation"]
        in (
            {"append", "update", "create"}
            if profile_for(semantic_profile).name == "records"
            else {"plan_add", "plan_update", "plan_triage", "plan_create"}
        )
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


def _valid_lifecycle_audit_event(event: Mapping[str, Any], semantic_profile: str) -> bool:
    if semantic_profile not in {"records", "planning"} or set(event) != {
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
        "continuity",
        "acknowledged_gap_codes",
        "gap_fingerprint",
        "checkpoint_snapshot_hash",
        "minimum_reader_version",
    }:
        return False
    hashes = (
        "before_manifest_hash",
        "after_manifest_hash",
        "before_container_hash",
        "after_container_hash",
        "payload_hash",
    )
    if not (
        type(event["version"]) is int
        and event["version"] == _LIFECYCLE_EVENT_VERSION
        and event["operation"]
        in (
            {"revise", "rebaseline"}
            if semantic_profile == "records"
            else {"plan_revise", "plan_rebaseline"}
        )
        and isinstance(event["transition_id"], str)
        and re.fullmatch(r"[0-9a-f]{24}", event["transition_id"]) is not None
        and isinstance(event["parent_id"], str)
        and (
            event["parent_id"] == "baseline"
            or re.fullmatch(r"[0-9a-f]{24}", event["parent_id"]) is not None
        )
        and _validate_audit_item_key(event["collection_id"]) is not None
        and all(
            _safe_audit_path(event[name])
            for name in ("manifest_path", "source_path", "canonical_path")
        )
        and event["canonical_path"] == event["manifest_path"]
        and event["item_key"] is None
        and event["before_item_hash"] is None
        and event["after_item_hash"] is None
        and all(
            isinstance(event[name], str) and re.fullmatch(r"[0-9a-f]{64}", event[name])
            for name in hashes
        )
        and isinstance(event["rationale"], str)
        and bool(event["rationale"].strip())
        and "\n" not in event["rationale"]
        and "\r" not in event["rationale"]
        and len(event["rationale"].encode("utf-8")) <= _MAX_WHY_BYTES
        and type(event["continuity"]) is bool
        and isinstance(event["acknowledged_gap_codes"], list)
        and all(
            type(code) is str and code and len(code.encode("utf-8")) <= 256
            for code in event["acknowledged_gap_codes"]
        )
        and type(event["minimum_reader_version"]) is int
        and event["minimum_reader_version"] == 2
    ):
        return False
    codes = event["acknowledged_gap_codes"]
    if event["operation"] == ("revise" if semantic_profile == "records" else "plan_revise"):
        return (
            event["continuity"] is True
            and codes == []
            and event["gap_fingerprint"] is None
            and event["checkpoint_snapshot_hash"] is None
        )
    return (
        event["continuity"] is False
        and codes == sorted(set(codes))
        and bool(codes)
        and all(
            isinstance(event[name], str) and re.fullmatch(r"[0-9a-f]{64}", event[name])
            for name in ("gap_fingerprint", "checkpoint_snapshot_hash")
        )
    )


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
    resolved = record_governance.resolve_collection_for_mutation(root, collection)
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
    """Validate one candidate, reporting every failing field in one refusal.

    The refusal keeps the code and message the raise-first path produced, so
    existing consumers are untouched; the complete field-addressed account rides
    in ``details.issues`` beside ``details.field`` for the first issue.
    """
    if not isinstance(item, Mapping):
        raise collections.CollectionError("INVALID_ITEM", "item must be an object")
    names = set(item)
    profile = profile_for(manifest.semantic_profile)
    system_fields = _SYSTEM_FIELDS - {"record_id"} | {profile.item_id_property}
    if names & system_fields:
        raise collections.CollectionError(
            "RESERVED_RECORD_FIELD", "item uses a reserved system field"
        )
    representational = _log_note_field(manifest)
    declared = set(manifest.schema.fields) | ({representational} if representational else set())
    issues: list[_Issue] = []
    for name in sorted(names - declared):
        issues.append(
            _Issue(
                field=name,
                code="SCHEMA_UNKNOWN_FIELD",
                reason=f"field is not declared: {name}",
                received=_value_class(item[name]),
                headline="item uses fields outside the schema",
            )
        )
    schema_values = {name: value for name, value in item.items() if name in manifest.schema.fields}
    value = _collect_schema_issues(manifest.schema, schema_values, issues)
    if representational:
        note = item.get(representational)
        if note is not None and type(note) is not str:
            issues.append(
                _Issue(
                    field=representational,
                    code="SCHEMA_FIELD_TYPE",
                    reason="heading note must be a string",
                    received=_value_class(note),
                )
            )
        value[representational] = note
    _collect_representation_issues(manifest, value, issues)
    if not issues and manifest.storage.strategy == "markdown-items":
        _collect_round_trip_issues(manifest, value, issues)
    if issues:
        raise _issue_refusal(issues)
    return value


@dataclass(frozen=True, slots=True)
class _Issue:
    """One field-addressed validation failure."""

    field: str
    code: str
    reason: str
    received: str
    #: The message the raise-first path used for this failure class. It differs
    #: from `reason` only where the shipped refusal spoke about the item rather
    #: than the field, and it is what the aggregated refusal still says.
    headline: str = ""
    #: Where the failure lives when no single field is at fault. The whole
    #: candidate frontmatter round-trips as one block, so naming an empty field
    #: would claim an address the failure does not have.
    scope: str = ""

    def as_detail(self) -> dict[str, str]:
        detail: dict[str, str] = {}
        if self.field:
            detail["field"] = self.field
        if self.scope:
            detail["scope"] = self.scope
        detail.update({"code": self.code, "reason": self.reason, "received": self.received})
        return detail


def _issue_refusal(issues: list[_Issue]) -> collections.CollectionError:
    first = issues[0]
    details: dict[str, Any] = {}
    if first.field:
        details["field"] = first.field
    if first.scope:
        details["scope"] = first.scope
    details["issues"] = [issue.as_detail() for issue in issues]
    return collections.CollectionError(first.code, first.headline or first.reason, details)


def _value_class(value: Any) -> str:
    """The received value's JSON class, never the value itself."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "str"
    if isinstance(value, Mapping):
        return "object"
    if isinstance(value, list | tuple):
        return "array"
    return type(value).__name__


def _collect_schema_issues(
    schema: collections.ItemSchema, values: Mapping[str, Any], issues: list[_Issue]
) -> dict[str, Any]:
    """Run the shipped per-field schema rules field by field, in declared order."""
    value = dict(values)
    for name, spec in schema.fields.items():
        if spec.required and name not in value:
            issues.append(
                _Issue(
                    field=name,
                    code="SCHEMA_REQUIRED_FIELD",
                    reason=f"required field is missing: {name}",
                    received="null",
                )
            )
        if name in value:
            _collect_field_issues(name, name, value[name], spec, issues)
    return value


def _collect_field_issues(
    path: str, name: str, value: Any, spec: collections.FieldSpec, issues: list[_Issue]
) -> None:
    """Address one field's failure, refining the path into arrays and objects.

    The shipped validator stays the authority on whether a value is acceptable;
    the refinement below only decides which sub-path to name.
    """
    try:
        collections.validate_field_value(name, value, spec)
        return
    except collections.CollectionError as error:
        failure = error
    located = _locate_field_failure(path, name, value, spec)
    if not located:
        issues.append(
            _Issue(
                field=path,
                code=failure.code,
                reason=failure.reason,
                received=_value_class(value),
            )
        )
        return
    for sub_path, sub_value, sub_failure in located:
        issues.append(
            _Issue(
                field=sub_path,
                code=sub_failure.code,
                reason=sub_failure.reason,
                received=_value_class(sub_value),
            )
        )


def _locate_field_failure(
    path: str, name: str, value: Any, spec: collections.FieldSpec
) -> list[tuple[str, Any, collections.CollectionError]]:
    if spec.type == "array" and isinstance(value, list) and spec.items is not None:
        found: list[tuple[str, Any, collections.CollectionError]] = []
        for index, entry in enumerate(value):
            try:
                collections.validate_field_value(name, entry, spec.items)
            except collections.CollectionError as error:
                deeper = _locate_field_failure(f"{path}[{index}]", name, entry, spec.items)
                found.extend(deeper or [(f"{path}[{index}]", entry, error)])
        return found
    if spec.type == "object" and isinstance(value, Mapping):
        offending = _non_json_path(path, value)
        if offending is not None and offending[0] != path:
            sub_path, sub_value = offending
            return [
                (
                    sub_path,
                    sub_value,
                    collections.CollectionError(
                        "SCHEMA_FIELD_TYPE", f"field has wrong type: {name}"
                    ),
                )
            ]
    return []


def _non_json_path(path: str, value: Any) -> tuple[str, Any] | None:
    """The path of the first value that cannot be represented as JSON."""
    if value is None or type(value) in {str, int, bool}:
        return None
    if type(value) is float:
        return None if math.isfinite(value) else (path, value)
    if isinstance(value, list | tuple):
        for index, item in enumerate(value):
            found = _non_json_path(f"{path}[{index}]", item)
            if found is not None:
                return found
        return None
    if isinstance(value, Mapping):
        for key, item in value.items():
            if type(key) is not str:
                return (path, value)
            found = _non_json_path(f"{path}.{key}", item)
            if found is not None:
                return found
        return None
    return (path, value)


def _log_note_field(manifest: collections.CollectionManifest) -> str | None:
    if manifest.storage.strategy != "markdown-log":
        return None
    heading = manifest.storage.descriptor.get("item_heading")
    note = heading.get("note") if isinstance(heading, Mapping) else None
    field = note.get("field") if isinstance(note, Mapping) else None
    return field if isinstance(field, str) else None


def _refuse_excluded_authored_names(names: object) -> None:
    if not isinstance(names, Mapping):
        return
    excluded = vault.first_excluded_field(names)
    if excluded is None:
        return
    field, reason = excluded
    raise collections.CollectionError(vault.EXCLUDED_FIELD_CODE, reason, details={"field": field})


def _refuse_excluded_manifest_fields(
    manifest: collections.CollectionManifest,
) -> None:
    excluded = vault.first_excluded_field(manifest.schema.fields)
    note_field = _log_note_field(manifest)
    if excluded is None and note_field is not None:
        excluded = vault.first_excluded_field((note_field,))
    if excluded is None:
        return
    field, reason = excluded
    raise collections.CollectionError(vault.EXCLUDED_FIELD_CODE, reason, details={"field": field})


def _collect_representation_issues(
    manifest: collections.CollectionManifest, values: Mapping[str, Any], issues: list[_Issue]
) -> None:
    """Judge representability against the collection's own storage strategy.

    Markdown-log rendering puts values into headings, notes and delimited child
    rows, none of which can carry a line break. Markdown-item frontmatter can:
    the writer's own quoting escapes one, and the round trip below proves it.
    """
    line_breaks_allowed = manifest.storage.strategy == "markdown-items"
    already = {issue.field for issue in issues}
    for name, value in values.items():
        if name in already:
            continue
        _collect_value_representation(name, value, line_breaks_allowed, issues)
    if manifest.storage.strategy == "markdown-log":
        _collect_log_grammar_issues(manifest, values, issues)


def _collect_log_grammar_issues(
    manifest: collections.CollectionManifest, values: Mapping[str, Any], issues: list[_Issue]
) -> None:
    """Refuse candidate values that carry the log grammar's own tokens.

    The heading separator, the note brackets and the row delimiter are as
    unrepresentable in this strategy as a line break; judging them here gives
    the caller the field path and lets the refusal hold the candidate.
    """
    tokens = record_formats.log_grammar_tokens(manifest)
    if tokens is None:
        return
    seen = {issue.field for issue in issues}

    def refuse(path: str, reason: str, value: Any) -> None:
        if path in seen:
            return
        seen.add(path)
        issues.append(_unrepresentable(path, reason, value))

    for name in tokens.heading_string_fields:
        value = values.get(name)
        if not isinstance(value, str):
            continue
        # The render layer refuses an empty heading string and a whitespace-only
        # note as unrenderable; judged here they carry a field path and hold.
        if not value:
            refuse(name, "a heading value cannot be empty", value)
        elif tokens.separator in value:
            refuse(name, "a heading value cannot carry the heading separator", value)
    note_field, opening, closing = tokens.note_field, tokens.note_open, tokens.note_close
    if note_field is not None:
        note = values.get(note_field)
        if isinstance(note, str):
            if not note.strip():
                refuse(note_field, "a heading note cannot be empty", note)
            elif opening and closing and (opening in note or closing in note):
                refuse(note_field, "a heading note cannot carry its own brackets", note)
    rows = values.get(tokens.container_field)
    if isinstance(rows, list):
        for index, row in enumerate(rows):
            if not isinstance(row, Mapping):
                continue
            for name in tokens.child_fields:
                cell = row.get(name)
                if isinstance(cell, str) and tokens.delimiter in cell:
                    refuse(
                        f"{tokens.container_field}[{index}].{name}",
                        "a child row value cannot carry the row delimiter",
                        cell,
                    )


def _collect_value_representation(
    path: str, value: Any, line_breaks_allowed: bool, issues: list[_Issue]
) -> None:
    if isinstance(value, str):
        if len(value.encode("utf-8")) > _MAX_VALUE_BYTES:
            issues.append(_unrepresentable(path, "record value exceeds the byte limit", value))
        elif not line_breaks_allowed and ("\r" in value or "\n" in value):
            issues.append(
                _unrepresentable(
                    path, "this storage strategy cannot carry a line break in a value", value
                )
            )
    elif isinstance(value, Mapping):
        for key, item in value.items():
            _collect_value_representation(f"{path}.{key}", item, line_breaks_allowed, issues)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _collect_value_representation(f"{path}[{index}]", item, line_breaks_allowed, issues)


def _unrepresentable(path: str, reason: str, value: Any) -> _Issue:
    return _Issue(
        field=path,
        code="UNREPRESENTABLE_RECORD_VALUE",
        reason=reason,
        received=_value_class(value),
        headline="record value cannot render losslessly",
    )


def _unrepresentable_candidate(reason: str, values: Mapping[str, Any]) -> _Issue:
    """A round-trip failure of the whole block, which no single field owns."""
    return _Issue(
        field="",
        code="UNREPRESENTABLE_RECORD_VALUE",
        reason=reason,
        received=_value_class(values),
        headline="record value cannot render losslessly",
        scope="frontmatter",
    )


def _collect_round_trip_issues(
    manifest: collections.CollectionManifest, values: Mapping[str, Any], issues: list[_Issue]
) -> None:
    """Prove a Markdown item reads back exactly what the caller supplied.

    Serialising the complete candidate frontmatter and parsing it with the reader
    the collection actually uses is the property we want, rather than a guess at
    which characters today's serializer can carry.
    """
    profile = profile_for(manifest.semantic_profile)
    frontmatter: dict[str, Any] = {
        "type": profile.item_type,
        "collection_id": manifest.collection_id,
        profile.item_id_property: collections.ItemIdentity(
            manifest.collection_id, _ROUND_TRIP_PROBE_KEY
        ).key,
        "schema_version": manifest.schema.version,
    }
    frontmatter.update(values)
    text = "---\n" + vault.serialize_frontmatter(frontmatter) + "\n---\n"
    try:
        parsed, _body, marker = vault.parse_frontmatter(text, strict=True)
    except vault.FrontmatterError:
        parsed, marker = {}, None
    if marker is None:
        issues.append(
            _unrepresentable_candidate("candidate frontmatter does not parse back", values)
        )
        return
    try:
        candidate = collections.normalize_item_values(manifest.schema, values)
        restored = collections.normalize_item_values(
            manifest.schema, {name: parsed.get(name) for name in values}
        )
    except collections.CollectionError:
        issues.append(
            _unrepresentable_candidate("candidate values do not normalize back", values)
        )
        return
    for name in values:
        if candidate.get(name) != restored.get(name):
            issues.append(
                _unrepresentable(name, "record value does not read back identically", values[name])
            )


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


# --------------------------------------------------------------------------
# Held records
#
# A candidate refused for its own content is an observation the user made. It is
# preserved as an ordinary human-owned Markdown file beside the items -- outside
# the item source, so adapter reads, queries, audit markers and recall pass over
# it without a single exclusion rule -- and can be resumed or discarded by
# reference. Holding never produces a second error: if it fails, the original
# refusal stands with a warning.
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class HeldCandidate:
    """One refused candidate preserved beside its collection."""

    held_id: str
    path: str
    collection_id: str
    attempted_action: str
    target_item_key: str | None
    held_at: str
    why: str
    candidate_sha256: str
    diagnostics: tuple[Mapping[str, Any], ...]
    candidate: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class HeldCensus:
    """What a collection's held directory holds for one audience."""

    #: Every authorized held file, counted from the listing.
    held: int
    #: Those that were opened and could not be loaded.
    unreadable: int
    #: Those that loaded, bounded by how many the census was willing to open.
    candidates: tuple[HeldCandidate, ...]


def hold_candidate(
    vault_root: Path,
    manifest: collections.CollectionManifest,
    *,
    attempted_action: str,
    candidate: Mapping[str, Any],
    why: str,
    issues: Sequence[Mapping[str, Any]] = (),
    key_values: Mapping[str, Any] | None = None,
    held_id: str | None = None,
    target_item_key: str | None = None,
) -> dict[str, Any]:
    """Write one refused candidate to `<collection dir>/Held/<held_id>.md`.

    Frontmatter is generated and single-line throughout, so a held file never
    depends on the representability rules that refused its candidate; the exact
    payload lives in one fenced JSON block instead.
    """
    root = Path(vault_root)
    directory = _held_directory(manifest)
    if not _held_directory_is_outside_source(manifest):
        raise collections.CollectionError(
            "HELD_DIRECTORY_UNSAFE",
            "held candidates cannot live under the collection's item source",
            {"held_directory": directory, "source": manifest.storage.source},
        )
    reference = held_id or _derive_held_id(manifest, key_values)
    relative = f"{directory}/{reference}.md"
    # The held file is a write into the vault, so it is subject to the same full
    # release the collection's own paths are. A caller who cannot see the held
    # subtree does not get to write into it; the soft-fail rule turns that into
    # the original refusal plus a warning rather than a second error.
    allowed = record_governance.full_release_filter(root)
    if not allowed(directory) or not allowed(relative):
        raise collections.CollectionError(
            "HELD_NOT_RELEASED",
            "held candidates cannot be written into a withheld path",
            {"held_directory": directory},
        )
    body = json.dumps(
        _encode_held_value(candidate),
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
        allow_nan=False,
        default=_held_json_default,
    )
    diagnostics = [dict(issue) for issue in list(issues)[:_MAX_HELD_DIAGNOSTICS]]
    frontmatter: dict[str, Any] = {
        "type": _HELD_TYPE,
        "collection_id": manifest.collection_id,
        "held_id": reference,
        "attempted_action": attempted_action,
        **({"target_item_key": target_item_key} if target_item_key else {}),
        "held_at": dt.datetime.now(tz=dt.UTC).isoformat().replace("+00:00", "Z"),
        "why": why,
        "candidate_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
        # A YAML list of objects cannot be rendered on one line, and the whole
        # point of the frontmatter is that it is single-line and generated. The
        # structured list rides as compact JSON text and reads back exactly.
        "diagnostics": json.dumps(diagnostics, ensure_ascii=False, separators=(",", ":")),
    }
    text = (
        "---\n"
        + vault.serialize_frontmatter(frontmatter)
        + "\n---\n\n```json\n"
        + body
        + "\n```\n"
    )
    vault.batch_atomic_write(
        [
            vault.PlannedWrite(
                root / relative,
                text,
                ensure_directories=(root / directory,),
            )
        ],
        vault_root=root,
    )
    return {"held_id": reference, "path": relative, "diagnostics": diagnostics}


def discard_held(
    vault_root: Path,
    collection: str | Path | collections.CollectionManifest,
    *,
    held: str,
    why: str,
) -> dict[str, Any]:
    """Remove one held candidate by reference, leaving the audit chain alone."""
    root = Path(vault_root)
    _validate_why(why)
    held_id = _validate_held_reference(held)
    if held_id is None:
        raise _held_not_found()
    with writer_lease.active_manager().mutation_guard(root, operation="record_discard"):
        manifest = record_governance.resolve_collection_for_mutation(root, collection)
        record_governance.require_mutation_visibility(root, manifest)
        candidate = _load_held_candidate(root, manifest, held_id)
        try:
            _remove_held_file(root, candidate.path)
        except OSError as error:
            raise collections.CollectionError(
                "HELD_NOT_REMOVED",
                "held candidate could not be removed",
                {"argument": "held"},
            ) from error
    return {
        "_record_receipt": _RECEIPT_MARKER,
        "receipt_version": _RECEIPT_VERSION,
        "operation": "discard",
        "collection_id": manifest.collection_id,
        "held_id": held_id,
        "affected_paths": [candidate.path],
        "outcome": "discarded",
        "audit_correlation": None,
    }


def held_census(
    vault_root: Path,
    manifest: collections.CollectionManifest,
    *,
    authorize_path: Callable[[str], bool] | None = None,
    load: bool = True,
) -> HeldCensus:
    """Count the authorized held candidates, and open them only if asked.

    The count comes from the directory listing, so it is exact however many
    files there are: a bound on how many candidates may be *named* is not a
    bound on how many exist, and reporting the smaller number told a caller the
    ledger was less blocked than it is. A file that is opened and does not load
    is counted as unreadable rather than silently dropped, and a caller that
    wants only a count passes `load=False`, which parses no candidate payload.
    """
    root = Path(vault_root)
    directory_name = _held_directory(manifest)
    directory = root / directory_name
    if not _held_directory_is_outside_source(manifest) or not directory.is_dir():
        return HeldCensus(0, 0, ())
    try:
        entries = sorted(entry.name for entry in directory.iterdir() if entry.is_file())
    except OSError:
        return HeldCensus(0, 0, ())
    references: list[str] = []
    for name in entries:
        if not name.endswith(".md"):
            continue
        reference = collections.memory_refs.normalize_id(name.removesuffix(".md"))
        if reference is None:
            continue
        if authorize_path is not None and not authorize_path(f"{directory_name}/{name}"):
            continue
        references.append(reference)
    if not load:
        return HeldCensus(len(references), 0, ())
    found: list[HeldCandidate] = []
    unreadable = 0
    for reference in references[:_MAX_HELD_LOADS]:
        try:
            found.append(_load_held_candidate(root, manifest, reference))
        except collections.CollectionError:
            unreadable += 1
    return HeldCensus(len(references), unreadable, tuple(found))


def _held_directory(manifest: collections.CollectionManifest) -> str:
    return (Path(manifest.path).parent / _HELD_DIRECTORY).as_posix()


def _held_directory_is_outside_source(manifest: collections.CollectionManifest) -> bool:
    """Whether `Held/` sits beside the items rather than inside them.

    `storage.source` may legally be the collection directory itself, which would
    put held files inside the directory the adapter walks and move the container
    hash. That collection simply cannot hold, and the caller soft-fails.
    """
    held = _portable_key(_held_directory(manifest))
    source = _portable_key(manifest.storage.source)
    return held != source and not held.startswith(f"{source}/")


def _portable_key(path: str) -> str:
    return "/".join(
        unicodedata.normalize("NFKC", part).casefold() for part in path.strip("/").split("/")
    )


def _derive_held_id(
    manifest: collections.CollectionManifest, values: Mapping[str, Any] | None
) -> str:
    """The identity a complete natural key implies, so a re-hold replays one file."""
    if isinstance(values, Mapping):
        try:
            derived = collections.derived_item_key(manifest, values)
        except (collections.CollectionError, ValueError, TypeError):
            derived = None
        if derived is not None:
            return derived
    return str(uuid.uuid4())


def _held_json_default(value: Any) -> str:
    if isinstance(value, dt.date | dt.datetime):
        return value.isoformat()
    raise TypeError(f"held candidate carries an unserializable value: {type(value).__name__}")


def _encode_held_value(value: Any) -> Any:
    """Tag non-finite floats so the fenced body stays strict, portable JSON.

    `NaN` and the infinities are legal Python floats and a perfectly ordinary
    reason for a `SCHEMA_FIELD_TYPE` refusal, but `json.dumps(allow_nan=False)`
    raises on them -- which lost the very candidate the hold exists to keep.
    """
    if type(value) is float and not math.isfinite(value):
        if math.isnan(value):
            return {_HELD_FLOAT_TAG: "NaN"}
        return {_HELD_FLOAT_TAG: "Infinity" if value > 0 else "-Infinity"}
    if isinstance(value, Mapping):
        encoded = {key: _encode_held_value(item) for key, item in value.items()}
        # A candidate object may legitimately have the exact shape of a tag. Wrap
        # it so decode never mistakes caller data for an encoder marker; the
        # wrapper itself is a reserved shape and is wrapped the same way.
        if set(value) in _HELD_RESERVED_SHAPES:
            return {_HELD_ESCAPE_TAG: encoded}
        return encoded
    if isinstance(value, list | tuple):
        return [_encode_held_value(item) for item in value]
    return value


def _decode_held_value(value: Any) -> Any:
    """Restore the tagged non-finite floats a resume must see unchanged."""
    if isinstance(value, Mapping):
        if set(value) == {_HELD_ESCAPE_TAG} and isinstance(value[_HELD_ESCAPE_TAG], Mapping):
            # An escaped literal: decode its children, never its own shape.
            return {key: _decode_held_value(item) for key, item in value[_HELD_ESCAPE_TAG].items()}
        if set(value) == {_HELD_FLOAT_TAG} and value[_HELD_FLOAT_TAG] in _HELD_NON_FINITE:
            return _HELD_NON_FINITE[value[_HELD_FLOAT_TAG]]
        return {key: _decode_held_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_decode_held_value(item) for item in value]
    return value


def _validate_hold(hold: object) -> bool:
    if type(hold) is not bool:
        raise collections.CollectionError("INVALID_RECORD_HOLD", "hold must be boolean")
    return hold


def _validate_held_reference(held: object) -> str | None:
    if held is None:
        return None
    normalized = collections.memory_refs.normalize_id(held) if isinstance(held, str) else None
    if normalized is None:
        raise _held_not_found()
    return normalized


def _held_not_found() -> collections.CollectionError:
    return collections.CollectionError(
        "HELD_NOT_FOUND", "held candidate does not exist", {"argument": "held"}
    )


def _load_held_candidate(
    root: Path, manifest: collections.CollectionManifest, held_id: str
) -> HeldCandidate:
    relative = f"{_held_directory(manifest)}/{held_id}.md"
    try:
        data, _guard = vault.read_bounded_guarded_bytes(root, relative, limit=_MAX_HELD_BYTES)
        text = data.decode("utf-8")
        frontmatter, body, marker = vault.parse_frontmatter(text, strict=True)
    except (vault.PathGuardError, vault.FrontmatterError, UnicodeDecodeError) as error:
        raise _held_not_found() from error
    if marker is None or frontmatter.get("type") != _HELD_TYPE:
        raise _held_not_found()
    if str(frontmatter.get("held_id", "")) != held_id:
        raise _held_not_found()
    stored_collection = collections.memory_refs.normalize_id(
        str(frontmatter.get("collection_id", ""))
    )
    if stored_collection != manifest.collection_id:
        raise collections.CollectionError(
            "HELD_COLLECTION_MISMATCH",
            "held candidate belongs to another collection",
            {"argument": "held"},
        )
    candidate = _held_candidate_payload(body)
    diagnostics = _held_diagnostics(frontmatter.get("diagnostics"))
    return HeldCandidate(
        held_id=held_id,
        path=relative,
        collection_id=manifest.collection_id,
        attempted_action=str(frontmatter.get("attempted_action", "")),
        target_item_key=(
            str(frontmatter["target_item_key"]) if frontmatter.get("target_item_key") else None
        ),
        held_at=str(frontmatter.get("held_at", "")),
        why=str(frontmatter.get("why", "")),
        candidate_sha256=str(frontmatter.get("candidate_sha256", "")),
        diagnostics=diagnostics,
        candidate=candidate,
    )


def _held_candidate_payload(body: str) -> Mapping[str, Any]:
    opening = body.find("```json")
    if opening < 0:
        raise _held_not_found()
    rest = body[opening + len("```json") :]
    closing = rest.find("```")
    if closing < 0:
        raise _held_not_found()
    try:
        payload = json.loads(rest[:closing])
    except ValueError as error:
        raise _held_not_found() from error
    if not isinstance(payload, Mapping):
        raise _held_not_found()
    return _decode_held_value(payload)


def _held_diagnostics(value: Any) -> tuple[Mapping[str, Any], ...]:
    if not isinstance(value, str):
        return ()
    try:
        parsed = json.loads(value)
    except ValueError:
        return ()
    if not isinstance(parsed, list):
        return ()
    return tuple(item for item in parsed if isinstance(item, Mapping))


def _remove_held_file(root: Path, relative: str) -> None:
    (root / relative).unlink()


def _holding_enabled(manifest: collections.CollectionManifest, hold: bool) -> bool:
    """Whether this collection may preserve a refused candidate at all.

    Only the Records profile can: `plan_memory` has no `held`, `hold` or
    `discard` argument, strips `details` from its refusals and reports no
    coverage, so a held Planning file would be a vault write the caller could
    neither see, list nor remove. Planning adopts the arguments in a later
    change; until then the shared writer refuses without a file.
    """
    return hold and manifest.semantic_profile == "records"


def _hold_refusal(
    root: Path,
    manifest: collections.CollectionManifest,
    error: collections.CollectionError,
    *,
    hold: bool,
    attempted_action: str,
    candidate: Mapping[str, Any],
    why: str,
    key_values: Mapping[str, Any] | None = None,
    held_id: str | None = None,
    target_item_key: str | None = None,
) -> collections.CollectionError:
    """Attach a held reference to a candidate-content refusal, or a warning."""
    if not _holding_enabled(manifest, hold) or error.code not in _CANDIDATE_CONTENT_CODES:
        return error
    details = dict(error.details)
    try:
        held = hold_candidate(
            root,
            manifest,
            attempted_action=attempted_action,
            candidate=candidate,
            why=why,
            issues=details.get("issues") or (),
            key_values=key_values,
            held_id=held_id,
            target_item_key=target_item_key,
        )
    except _HOLD_FAILURES as failure:
        log.warning("held candidate was not written: %s", type(failure).__name__)
        details["warnings"] = [_HOLD_FAILED_WARNING]
        return collections.CollectionError(error.code, error.reason, details)
    details["held"] = held
    return collections.CollectionError(error.code, error.reason, details)


def _apply_held_overrides(
    base: Mapping[str, Any], overrides: Mapping[str, Any]
) -> dict[str, Any]:
    """Shallow overrides onto a held candidate; `null` removes the field."""
    merged = dict(base)
    for name, value in overrides.items():
        if value is None:
            merged.pop(name, None)
        else:
            merged[name] = value
    return merged


def _held_mapping(candidate: Mapping[str, Any], name: str) -> dict[str, Any]:
    value = candidate.get(name)
    if not isinstance(value, Mapping):
        raise _held_not_found()
    return dict(value)


def _held_delete_fields(resumed: HeldCandidate) -> tuple[str, ...]:
    stored = resumed.candidate.get("delete_fields")
    if not isinstance(stored, list):
        return ()
    return tuple(name for name in stored if isinstance(name, str))


def _resumed_update_body(resumed: HeldCandidate, body: str | None) -> str | None:
    """`None` still means "leave the body alone" on a resumed update."""
    if body is not None:
        return body
    stored = resumed.candidate.get("body")
    return stored if isinstance(stored, str) else None


def _resumed_body(resumed: HeldCandidate | None, body: str | None) -> str:
    """The caller's body wins; an omitted one falls back to the held candidate's."""
    if body is not None:
        return body
    if resumed is None:
        return ""
    stored = resumed.candidate.get("body")
    return stored if isinstance(stored, str) else ""


def _with_warnings(result: dict[str, Any], warnings: list[str]) -> dict[str, Any]:
    """Attach warnings only when there are any, so the ordinary shape is unchanged."""
    return {**result, "warnings": list(warnings)} if warnings else result


def _release_held_file(root: Path, resumed: HeldCandidate | None, warnings: list[str]) -> None:
    """Remove a resumed held file after its item committed, or warn and move on.

    The item stands either way: a leftover held file is inert Markdown the caller
    can discard, and turning it into a failure would undo a good commit.
    """
    if resumed is None:
        return
    try:
        _remove_held_file(root, resumed.path)
    except OSError:
        warnings.append(_HELD_CLEANUP_WARNING)


def _validate_item_key(
    key: str,
    *,
    manifest: collections.CollectionManifest | None = None,
    candidate: Mapping[str, Any] | None = None,
) -> str:
    normalized = collections.memory_refs.normalize_id(key)
    if normalized is None:
        raise _invalid_item_key(key, manifest, candidate)
    return normalized


def _validate_update_item_key(
    root: Path,
    collection: str | Path | collections.CollectionManifest,
    item_key: Any,
    changes: Any,
) -> str:
    """Normalize an update's item key, resolving the collection only to remediate.

    The collection is opened only once the key is already known to be wrong, and
    only through the mutation resolver, so an unreadable or withheld collection
    still refuses the key without disclosing that it exists.
    """
    normalized = (
        collections.memory_refs.normalize_id(item_key) if isinstance(item_key, str) else None
    )
    if normalized is not None:
        return normalized
    try:
        manifest: collections.CollectionManifest | None = (
            record_governance.resolve_collection_for_mutation(root, collection)
        )
    except (collections.CollectionError, OSError, ValueError):
        manifest = None
    raise _invalid_item_key(item_key, manifest, changes if isinstance(changes, Mapping) else None)


def _invalid_item_key(
    key: Any,
    manifest: collections.CollectionManifest | None,
    candidate: Mapping[str, Any] | None,
) -> collections.CollectionError:
    """Refuse a non-UUID item key, naming the natural key when that is the mistake.

    A caller who supplies the observation's own natural-key value as `item_key`
    is not guessing a UUID badly; it has mistaken the internal identity for the
    key it already knows, and the only useful answer names both.
    """
    details: dict[str, Any] = {"argument": "item_key", "received": _received_item_key(key)}
    if manifest is not None and _reads_as_natural_key(manifest, candidate, key):
        details["natural_key"] = list(manifest.schema.natural_key)
        details["remediation"] = _ITEM_KEY_REMEDIATION
    return collections.CollectionError("INVALID_RECORD_ID", "record ID must be a UUID", details)


def _received_item_key(key: Any) -> str:
    if type(key) is not str:
        return _value_class(key)
    return key if len(key) <= _MAX_RECEIVED_KEY_CHARS else key[:_MAX_RECEIVED_KEY_CHARS]


def _reads_as_natural_key(
    manifest: collections.CollectionManifest,
    candidate: Mapping[str, Any] | None,
    key: Any,
) -> bool:
    declared = manifest.schema.natural_key
    if not declared or not isinstance(candidate, Mapping):
        return False
    if all(candidate.get(name) is not None for name in declared):
        return True
    return any(
        candidate.get(name) is not None and str(candidate[name]) == key for name in declared
    )


def _natural_key_twins(
    manifest: collections.CollectionManifest,
    snapshot: record_formats.AdapterSnapshot,
    key: str,
    values: Mapping[str, Any],
) -> list[str]:
    """Existing items holding this natural key under a different identity.

    The replay rules compare identities, so nothing in them can see an item that
    a pre-derivation `uuid4` key is hiding; without this the collection would
    hold one observation twice under two identities and neither would be wrong.
    The scan runs over the snapshot the append has already loaded.
    """
    try:
        serialized = collections.manifest_natural_key(manifest, values)
    except ValueError:
        return []
    twins = {
        record.identity.key
        for record in snapshot.records
        if record.identity.key != key and _same_natural_key(manifest, record.values, serialized)
    }
    return sorted(twins)


def _natural_key_moved(
    manifest: collections.CollectionManifest,
    before: Mapping[str, Any],
    after: Mapping[str, Any],
) -> bool:
    """Whether this update changes the item's own declared natural key.

    Only a write that MOVES an item onto a key is capable of creating a twin, so
    only that write is refused. An update that leaves the key alone is judged by
    nothing here -- otherwise a vault that already holds twins from before the
    check existed could not be edited at all, including by the very edit the
    refusal's remediation asks for.
    """
    try:
        return collections.manifest_natural_key(
            manifest, before
        ) != collections.manifest_natural_key(manifest, after)
    except ValueError:
        # One side carries no natural key at all. It cannot collide, and it
        # cannot be moved onto a key it does not have.
        return False


def _same_natural_key(
    manifest: collections.CollectionManifest, values: Mapping[str, Any], serialized: str
) -> bool:
    try:
        return collections.manifest_natural_key(manifest, values) == serialized
    except ValueError:
        # A stored item missing a declared natural-key field carries no natural
        # key at all, so it cannot collide with one.
        return False


def _expect_hash(expected: str | None, actual: str, kind: str) -> None:
    if type(expected) is not str or expected != actual:
        raise collections.CollectionError("STALE_RECORD", f"{kind} version is stale")


def _publication_error(error: Exception) -> collections.CollectionError:
    if isinstance(error, vault.PathGuardError):
        if error.code in {"BATCH_RESIDUE_LIMIT", "BATCH_RESIDUE_UNSAFE"}:
            return collections.CollectionError(
                "RECORD_RECOVERY_REQUIRED",
                "private transaction residue blocks safe publication",
            )
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


def _presentation_relationship_resolver(
    root: Path,
    manifest: collections.CollectionManifest,
    snapshot: record_formats.AdapterSnapshot,
) -> Callable[[str, str], tuple[str, str] | None] | None:
    return record_formats.presentation_relationship_resolver(
        root,
        manifest,
        snapshot,
        authorize_path=record_governance.full_release_filter(root),
    )


def _new_item_path(
    root: Path,
    manifest: collections.CollectionManifest,
    key: str,
    values: Mapping[str, Any],
    snapshot: record_formats.AdapterSnapshot,
) -> tuple[Path, tuple[vault.DirectoryCensusGuard, ...]]:
    source = root / manifest.storage.source
    source_guard = next(
        (guard for guard in snapshot.directory_guards if guard.target == manifest.storage.source),
        None,
    )
    if source_guard is None or source_guard.directory_identity is None:
        raise collections.CollectionError("SOURCE_NOT_FOUND", "item directory could not be read")
    if manifest.item_filename is not None:
        occupied_paths = {record.source.path for record in snapshot.records} | {
            f"{manifest.storage.source.rstrip('/')}/{entry.relative_path}"
            for entry in source_guard.entries
            if not stat.S_ISDIR(entry.mode)
        }
        relative = collections.render_item_path(
            manifest,
            values,
            key,
            occupied_paths=occupied_paths,
        )
        target = root / relative
    else:
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
    profile = profile_for(manifest.semantic_profile)
    plan = vault.plan_log_writes(
        root,
        date_iso=dt.date.today().isoformat(),
        op="record_memory" if profile.name == "records" else "plan_memory",
        rel_path_no_ext=manifest.storage.source.removesuffix(".md"),
        body=body,
        operation_token=f"{profile.name}:{manifest.collection_id}:{key}:{token_hash}",
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
    profile = profile_for(manifest.semantic_profile)
    if profile.name == "planning":
        operation = {
            "create": "plan_create",
            "append": "plan_add",
            "update": "plan_update",
            "triage": "plan_triage",
        }.get(operation, operation)
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
    return profile.activity_prefix + json.dumps(
        event, ensure_ascii=False, separators=(",", ":"), allow_nan=False, sort_keys=True
    )


def _payload_hash(
    manifest: collections.CollectionManifest, key: str, values: Mapping[str, Any], body: str
) -> str:
    ordered = [[name, _normalize_json(values[name])] for name in sorted(values)]
    payload = [
        manifest.schema.version,
        key,
        ordered,
        _normalize_json(_semantic_body(body, manifest, values)),
    ]
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode(
            "utf-8"
        )
    ).hexdigest()


def _semantic_body(
    body: str,
    manifest: collections.CollectionManifest | None = None,
    values: Mapping[str, Any] | None = None,
) -> str:
    """Ignore one valid renderer span when comparing append retries."""
    try:
        span = record_formats._presentation_span(body)
    except collections.CollectionError:
        return body
    if span is None:
        return body
    if manifest is not None and values is not None:
        payload = record_formats._presentation_payload(manifest, values)
        canonical = json.dumps(
            payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
        expected_digest = hashlib.sha256(canonical).hexdigest()
        marker = record_formats._PRESENTATION_OPEN.search(body, span[0], span[1])
        if marker is None or marker.group(1) != expected_digest:
            return body
    rendered = body[: span[0]] + body[span[1] :]
    # Remove only the renderer's structural separator.  Author-authored leading
    # blank lines remain part of semantic append identity.
    if span[0] == 0:
        if rendered in {"\n", "\r\n"}:
            return ""
        if rendered.startswith("\r\n\r\n"):
            return rendered[4:]
        if rendered.startswith("\n\n"):
            return rendered[2:]
    return rendered


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


def _absent_source_container_hash(manifest: collections.CollectionManifest) -> str:
    """Bind a create transition to a deliberately absent declared source."""
    return hashlib.sha256(
        json.dumps(
            [
                (manifest.manifest_version.path, manifest.manifest_version.hash),
                ("absent-source", manifest.storage.strategy, manifest.storage.source),
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


def _due_state_carrier(
    root: Path,
    manifest: collections.CollectionManifest,
    *,
    path: str,
    key: str,
    values: Mapping[str, Any],
    previous: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    """The advisory block this structured write may carry (design D7).

    The same two helpers the page-write seam uses, for the same reason: the write
    that opens a gap is the one response that can report it, inside the same turn
    and on every client. Emission is decided at the mutation terminal, which is
    also where a page write's block is decided, so a multi-write command still
    emits at most once.

    Called AFTER the mutation guard is released, exactly where
    `semantic_writes.commit_existing` calls its own. The bytes are committed and
    the receipt is composed; the projection is derived state that no other writer
    is waiting on, and holding the writer lease across it made 31-40% of the lock
    hold time an advisory nobody had asked for.

    A failure here is never allowed to fail the commit: the bytes are already
    written, and an advisory count is not worth a receipt.
    """
    try:
        from . import due_state

        block = due_state.block_for_structured_write(
            root, manifest, path=path, key=key, values=values, previous=previous
        )
        if not block:
            return None
        # Server-internal: the terminal reads the vault for the emission key and
        # never puts it on the wire.
        return {**block, "_vault": str(root)}
    except Exception:  # noqa: BLE001 -- a due-state count never breaks a commit
        log.debug("structured due-state projection failed (non-fatal)", exc_info=True)
        return None


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
    profile = profile_for(manifest.semantic_profile)
    if profile.name == "planning":
        operation = {"append": "add"}.get(operation, operation)
    return {
        "_record_receipt" if profile.name == "records" else "_plan_receipt": (
            _RECEIPT_MARKER if profile.name == "records" else "exomem.planning-mutation"
        ),
        "receipt_version": _RECEIPT_VERSION,
        "operation": operation,
        "collection_id": manifest.collection_id,
        "item_key" if profile.name == "records" else "plan_id": key,
        "before_item_hash": before_item_hash,
        "after_item_hash": after_item_hash,
        "before_container_hash": before_container_hash,
        "after_container_hash": after_container_hash,
        "affected_paths": affected_paths,
        "payload_hash": payload_hash,
        "outcome": outcome,
        "audit_correlation": audit_correlation,
    }
