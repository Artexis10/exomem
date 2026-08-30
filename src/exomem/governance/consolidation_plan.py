"""Canonical, immutable joint plans for governed vault consolidation.

The plan digest binds content, policy, principals, disclosure expectations,
verification, rollback, retention, impact, and trusted rendering definition in
one RFC 8785/JCS preimage.  Approval and persistence are deliberately separate
layers: this module is pure and cannot mint authority or touch a vault.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import PurePosixPath
from types import MappingProxyType
from typing import NoReturn

from .projections import ProjectionCanonicalizationError, canonical_jcs

PLAN_SCHEMA = "exomem.consolidation-plan/v1"
CONTROL_BASIS_SCHEMA = "exomem.consolidation-control-basis/v1"
PLAN_INPUT_SET_SCHEMA = "exomem.consolidation-plan-input-set/v1"
PLAN_SUCCESSOR_AUTOMATON_SCHEMA = "exomem.consolidation-plan-successor-automaton/v1"
JOURNAL_BATCH_PARTITION_SCHEMA = "exomem.consolidation-journal-batch-partition/v1"
MAX_CONTENT_BATCH_ACTIONS = 128

_PLAN_DOMAIN = PLAN_SCHEMA.encode("ascii")
_CONTROL_BASIS_DOMAIN = CONTROL_BASIS_SCHEMA.encode("ascii")
_PLAN_INPUT_SET_DOMAIN = PLAN_INPUT_SET_SCHEMA.encode("ascii")
_AUTOMATON_DOMAIN = PLAN_SUCCESSOR_AUTOMATON_SCHEMA.encode("ascii")
_JOURNAL_BATCH_PARTITION_DOMAIN = JOURNAL_BATCH_PARTITION_SCHEMA.encode("ascii")
_BATCH_ACTION_SET_DOMAIN = b"exomem.consolidation-content-batch-actions/v1"
_BATCH_PRIOR_DOMAIN = b"exomem.consolidation-content-batch-prior/v1"
_BATCH_PREPARED_DOMAIN = b"exomem.consolidation-content-batch-prepared/v1"
_BATCH_FINAL_DOMAIN = b"exomem.consolidation-content-batch-final/v1"
_IMPACT_SUMMARY_DOMAIN = b"exomem.consolidation-impact-summary/v1"
_RENDERING_DEFINITION_DOMAIN = b"exomem.consolidation-rendering-definition/v1"
_RENDER_SECTION_DOMAIN = b"exomem.consolidation-render-section/v1"
_RENDER_PAGE_DOMAIN = b"exomem.consolidation-render-page/v1"

_MAX_SAFE_INTEGER = (1 << 53) - 1
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,511}\Z")
_UUID4 = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z")
_RFC3339_MILLISECONDS = re.compile(
    r"[0-9]{4}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12][0-9]|3[01])"
    r"T(?:[01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]\.[0-9]{3}Z\Z"
)
_PLAN_KINDS = frozenset({"cutover", "retirement", "rollback"})
_RUN_MODES = frozenset({"cloned-rehearsal", "real-cutover"})
_CONTENT_ACTIONS = frozenset(
    {
        "add",
        "deduplicate_exact",
        "overwrite",
        "relocate_preserving_bytes",
        "remove",
        "reuse_destination",
    }
)
_SECTION_IDS = (
    "impact-summary",
    "content-actions",
    "policy",
    "principals-disclosure",
    "verification",
    "rollback-retention",
)
RENDER_SECTION_IDS = _SECTION_IDS
RENDER_PAGE_SIZE = 20

_BASE_FIELDS = frozenset(
    {
        "schema",
        "protocol_version",
        "plan_kind",
        "run_id",
        "run_mode",
        "source_snapshot_fingerprint",
        "destination_snapshot_fingerprint",
        "expected_destination_preimage_census_digest",
        "source_inventory_digest",
        "reconciliation_digest",
        "conflict_decision_digest",
        "identity_map_digest",
        "path_map_digest",
        "dependency_map_digest",
        "content_actions",
        "journal_batch_partition_digest",
        "policy_documents",
        "policy_bundle_digest",
        "prospective_policy_fingerprint",
        "bridge_fingerprints",
        "exact_release_approval_fingerprints",
        "principal_attestation_set_digest",
        "disclosure_matrix_digest",
        "verification_plan",
        "rollback_contingency",
        "source_retention",
        "plan_successor_automaton_digest",
        "impact_summary",
        "rendering_definition",
        "created_at",
        "valid_until",
        "nonce",
    }
)
_PLAN_FIELDS = _BASE_FIELDS | {"control_basis_digest"}
_CONTROL_FIELDS = frozenset(
    {
        "schema",
        "run_id",
        "plan_kind",
        "plan_materialization_operation_id",
        "basis_run_revision",
        "source_snapshot_fingerprint",
        "destination_snapshot_fingerprint",
        "plan_input_set_digest",
        "plan_nonce",
        "predecessor_event_id",
        "predecessor_payload_digest",
        "plan_successor_automaton_digest",
    }
)
_ACTION_FIELDS = frozenset(
    {
        "ordinal",
        "batch_ordinal",
        "action",
        "object_ref",
        "source_path",
        "destination_path",
        "expected_before_state",
        "expected_before_sha256",
        "planned_after_state",
        "planned_after_sha256",
    }
)
_POLICY_DOCUMENT_FIELDS = frozenset({"path", "content", "byte_size", "sha256"})
_VERIFICATION_FIELDS = frozenset({"schema", "positive_probe_digest", "negative_probe_digest"})
_ROLLBACK_FIELDS = frozenset(
    {
        "schema",
        "contingency_digest",
        "applies_to",
        "recovery_window_deadline",
        "future_terminal_plan_authorized",
    }
)
_RETENTION_FIELDS = frozenset(
    {
        "schema",
        "state",
        "recovery_window_deadline",
        "recovery_window_ttl_ms",
        "surviving_copy_required",
    }
)
_IMPACT_FIELDS = frozenset(
    {
        "schema",
        "create_count",
        "overwrite_count",
        "removal_count",
        "relocation_count",
        "deduplication_count",
        "provenance_mapping_count",
        "policy_change_count",
        "principal_change_count",
        "disclosure_change_count",
        "batch_count",
        "rollback_consequence_count",
        "surviving_copy_obligation_count",
        "unresolved_count",
        "rollback_consequence",
        "surviving_copy_obligation",
    }
)
_RENDERING_FIELDS = frozenset({"schema", "page_size", "page_count", "total_rows", "sections"})
_SECTION_FIELDS = frozenset(
    {
        "ordinal",
        "section_id",
        "row_count",
        "first_page_ordinal",
        "page_count",
        "content_digest",
    }
)

__all__ = [
    "CONTROL_BASIS_SCHEMA",
    "JOURNAL_BATCH_PARTITION_SCHEMA",
    "MAX_CONTENT_BATCH_ACTIONS",
    "PLAN_INPUT_SET_SCHEMA",
    "PLAN_SCHEMA",
    "PLAN_SUCCESSOR_AUTOMATON_SCHEMA",
    "CanonicalConsolidationPlan",
    "CanonicalObject",
    "CanonicalPlanPage",
    "ConsolidationPlanUnavailable",
    "PlanMaterializationContext",
    "RENDER_PAGE_SIZE",
    "RENDER_SECTION_IDS",
    "canonical_closed_jcs",
    "derive_journal_batch_partition",
    "derive_rendering_definition",
    "materialize_plan",
    "parse_canonical_plan",
    "parse_control_basis",
    "parse_journal_batch_partition",
    "plan_successor_automaton",
    "render_plan_page",
]


class ConsolidationPlanUnavailable(RuntimeError):
    """Stable, content-free refusal for malformed or stale plan material."""

    code = "CONSOLIDATION_PLAN_UNAVAILABLE"

    def __init__(self) -> None:
        super().__init__("consolidation plan is unavailable")


@dataclass(frozen=True, slots=True)
class CanonicalObject:
    preimage: Mapping[str, object]
    canonical_bytes: bytes
    framed_bytes: bytes
    digest: str


@dataclass(frozen=True, slots=True)
class PlanMaterializationContext:
    operation_id: str
    basis_run_revision: int
    predecessor_event_id: str
    predecessor_payload_digest: str


@dataclass(frozen=True, slots=True)
class CanonicalConsolidationPlan:
    preimage: Mapping[str, object]
    canonical_bytes: bytes
    framed_bytes: bytes
    digest: str
    plan_input_set_digest: str
    control_basis: CanonicalObject
    impact_summary_digest: str
    rendering_definition_digest: str


@dataclass(frozen=True, slots=True)
class CanonicalPlanPage:
    plan_digest: str
    plan_kind: str
    run_id: str
    page_ordinal: int
    section_id: str
    section_page_ordinal: int
    total_pages: int
    total_rows: int
    section_row_count: int
    page_row_start: int
    page_row_stop: int
    impact_summary: Mapping[str, object]
    rows: tuple[Mapping[str, object], ...]
    canonical_bytes: bytes
    digest: str


def _fail() -> NoReturn:
    raise ConsolidationPlanUnavailable from None


def _normalize_text(value: object) -> str:
    if not isinstance(value, str):
        _fail()
    normalized = unicodedata.normalize("NFC", value)
    try:
        normalized.encode("utf-8")
    except UnicodeEncodeError:
        _fail()
    return normalized


def _normalize_value(value: object) -> object:
    if value is None or isinstance(value, float):
        _fail()
    if value is True or value is False:
        return value
    if isinstance(value, int):
        if value < 0 or value > _MAX_SAFE_INTEGER:
            _fail()
        return value
    if isinstance(value, str):
        return _normalize_text(value)
    if isinstance(value, Mapping):
        normalized: dict[str, object] = {}
        for raw_key, item in value.items():
            key = _normalize_text(raw_key)
            if key in normalized:
                _fail()
            normalized[key] = _normalize_value(item)
        return normalized
    if isinstance(value, (list, tuple)):
        return tuple(_normalize_value(item) for item in value)
    _fail()


def _freeze(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


def _canonical_normalized(value: object) -> bytes:
    try:
        return canonical_jcs(value)
    except ProjectionCanonicalizationError:
        _fail()


def canonical_closed_jcs(value: object) -> bytes:
    """Canonicalize the non-null, non-negative interoperable plan subset."""

    return _canonical_normalized(_normalize_value(value))


def _frame(domain: bytes, payload: bytes) -> bytes:
    return len(domain).to_bytes(4, "big") + domain + len(payload).to_bytes(8, "big") + payload


def _canonical_object(value: Mapping[str, object], domain: bytes) -> CanonicalObject:
    normalized = _normalize_value(value)
    if not isinstance(normalized, Mapping):
        _fail()
    frozen = _freeze(normalized)
    if not isinstance(frozen, Mapping):
        _fail()
    canonical = _canonical_normalized(normalized)
    framed = _frame(domain, canonical)
    return CanonicalObject(
        preimage=frozen,
        canonical_bytes=canonical,
        framed_bytes=framed,
        digest=hashlib.sha256(framed).hexdigest(),
    )


def _mapping(value: object, fields: frozenset[str]) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or frozenset(value) != fields:
        _fail()
    return value


def _sequence(value: object, *, maximum: int = 100_000) -> Sequence[object]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        _fail()
    if len(value) > maximum:
        _fail()
    return value


def _integer(value: object, *, minimum: int = 0, maximum: int = _MAX_SAFE_INTEGER) -> int:
    if type(value) is not int or value < minimum or value > maximum:
        _fail()
    return value


def _identifier(value: object) -> str:
    text = _normalize_text(value)
    if _IDENTIFIER.fullmatch(text) is None:
        _fail()
    return text


def _uuid4(value: object) -> str:
    text = _normalize_text(value)
    if _UUID4.fullmatch(text) is None:
        _fail()
    return text


def _digest(value: object) -> str:
    text = _normalize_text(value)
    if _DIGEST.fullmatch(text) is None:
        _fail()
    return text


def _timestamp(value: object) -> tuple[str, datetime]:
    text = _normalize_text(value)
    if _RFC3339_MILLISECONDS.fullmatch(text) is None:
        _fail()
    try:
        parsed = datetime.fromisoformat(text.removesuffix("Z") + "+00:00")
    except ValueError:
        _fail()
    if parsed.tzinfo != UTC:
        _fail()
    return text, parsed


def _path(value: object) -> str:
    text = _normalize_text(value)
    if not text or "\\" in text or "\x00" in text:
        _fail()
    candidate = PurePosixPath(text)
    if (
        candidate.is_absolute()
        or candidate.as_posix() != text
        or any(part in {"", ".", ".."} for part in text.split("/"))
    ):
        _fail()
    return text


def _sorted_digests(value: object) -> tuple[str, ...]:
    values = tuple(_digest(item) for item in _sequence(value, maximum=4096))
    if len(set(values)) != len(values):
        _fail()
    return tuple(sorted(values))


def _validate_content_actions(value: object) -> tuple[Mapping[str, object], ...]:
    actions = _sequence(value)
    if not actions:
        _fail()
    normalized: list[Mapping[str, object]] = []
    for expected_ordinal, raw in enumerate(actions):
        item = _mapping(raw, _ACTION_FIELDS)
        if _integer(item["ordinal"]) != expected_ordinal:
            _fail()
        _integer(item["batch_ordinal"], maximum=(1 << 31) - 1)
        action = _identifier(item["action"])
        if action not in _CONTENT_ACTIONS:
            _fail()
        _identifier(item["object_ref"])
        _path(item["source_path"])
        _path(item["destination_path"])
        before = _identifier(item["expected_before_state"])
        after = _identifier(item["planned_after_state"])
        if before not in {"absent", "present"} or after not in {"absent", "present"}:
            _fail()
        before_hash = _digest(item["expected_before_sha256"])
        after_hash = _digest(item["planned_after_sha256"])
        if before == "absent" and before_hash != "0" * 64:
            _fail()
        if after == "absent" and after_hash != "0" * 64:
            _fail()
        normalized.append(item)
    return tuple(normalized)


def validate_content_actions(value: object) -> tuple[Mapping[str, object], ...]:
    """Return the exact validated action sequence used by plan fingerprints."""

    return _validate_content_actions(value)


def derive_journal_batch_partition(value: object) -> CanonicalObject:
    """Derive the only approved content-batch partition and state fingerprints."""

    actions = validate_content_actions(value)
    grouped: list[list[Mapping[str, object]]] = []
    expected_batch = 0
    for action in actions:
        batch_ordinal = _integer(action["batch_ordinal"], maximum=(1 << 31) - 1)
        if batch_ordinal == expected_batch:
            grouped.append([action])
            expected_batch += 1
        elif batch_ordinal == expected_batch - 1 and grouped:
            grouped[-1].append(action)
        else:
            _fail()

    batches: list[dict[str, object]] = []
    for batch_ordinal, batch_actions in enumerate(grouped):
        if len(batch_actions) > MAX_CONTENT_BATCH_ACTIONS:
            _fail()
        action_set = _canonical_object(
            {
                "schema": "exomem.consolidation-content-batch-actions/v1",
                "batch_ordinal": batch_ordinal,
                "actions": tuple(batch_actions),
            },
            _BATCH_ACTION_SET_DOMAIN,
        )
        prior_rows = tuple(
            {
                "ordinal": action["ordinal"],
                "object_ref": action["object_ref"],
                "destination_path": action["destination_path"],
                "state": action["expected_before_state"],
                "sha256": action["expected_before_sha256"],
            }
            for action in batch_actions
        )
        final_rows = tuple(
            {
                "ordinal": action["ordinal"],
                "object_ref": action["object_ref"],
                "destination_path": action["destination_path"],
                "state": action["planned_after_state"],
                "sha256": action["planned_after_sha256"],
            }
            for action in batch_actions
        )
        prior = _canonical_object(
            {
                "schema": "exomem.consolidation-content-batch-prior/v1",
                "batch_ordinal": batch_ordinal,
                "entries": prior_rows,
            },
            _BATCH_PRIOR_DOMAIN,
        )
        prepared = _canonical_object(
            {
                "schema": "exomem.consolidation-content-batch-prepared/v1",
                "batch_ordinal": batch_ordinal,
                "entries": final_rows,
            },
            _BATCH_PREPARED_DOMAIN,
        )
        final = _canonical_object(
            {
                "schema": "exomem.consolidation-content-batch-final/v1",
                "batch_ordinal": batch_ordinal,
                "entries": final_rows,
            },
            _BATCH_FINAL_DOMAIN,
        )
        batches.append(
            {
                "batch_ordinal": batch_ordinal,
                "first_action_ordinal": batch_actions[0]["ordinal"],
                "last_action_ordinal": batch_actions[-1]["ordinal"],
                "action_count": len(batch_actions),
                "publication_boundary": batch_ordinal == 0,
                "action_set_digest": action_set.digest,
                "prior_fingerprint": prior.digest,
                "prepared_fingerprint": prepared.digest,
                "final_fingerprint": final.digest,
            }
        )
    return _canonical_object(
        {
            "schema": JOURNAL_BATCH_PARTITION_SCHEMA,
            "action_count": len(actions),
            "batch_count": len(batches),
            "batches": tuple(batches),
        },
        _JOURNAL_BATCH_PARTITION_DOMAIN,
    )


def _validate_policy_documents(value: object) -> tuple[Mapping[str, object], ...]:
    documents = _sequence(value, maximum=1024)
    normalized: list[Mapping[str, object]] = []
    paths: set[str] = set()
    folded: set[str] = set()
    for raw in documents:
        item = _mapping(raw, _POLICY_DOCUMENT_FIELDS)
        path = _path(item["path"])
        parsed = PurePosixPath(path)
        if parsed.parts[0] not in {"grants", "rules", "scopes"} or parsed.suffix not in {
            ".yaml",
            ".yml",
        }:
            _fail()
        if path in paths or path.casefold() in folded:
            _fail()
        content = _normalize_text(item["content"])
        encoded = content.encode("utf-8")
        if len(encoded) > 1 << 20:
            _fail()
        if _integer(item["byte_size"]) != len(encoded):
            _fail()
        if _digest(item["sha256"]) != hashlib.sha256(encoded).hexdigest():
            _fail()
        paths.add(path)
        folded.add(path.casefold())
        normalized.append(item)
    if tuple(item["path"] for item in normalized) != tuple(sorted(paths)):
        _fail()
    return tuple(normalized)


def _validate_verification(value: object) -> Mapping[str, object]:
    item = _mapping(value, _VERIFICATION_FIELDS)
    if item["schema"] != "exomem.consolidation-verification-plan/v1":
        _fail()
    _digest(item["positive_probe_digest"])
    _digest(item["negative_probe_digest"])
    return item


def _validate_rollback(value: object) -> tuple[Mapping[str, object], datetime]:
    item = _mapping(value, _ROLLBACK_FIELDS)
    if (
        item["schema"] != "exomem.consolidation-rollback-contingency/v1"
        or item["applies_to"] != "nonterminal-apply"
        or item["future_terminal_plan_authorized"] is not False
    ):
        _fail()
    _digest(item["contingency_digest"])
    _deadline_text, deadline = _timestamp(item["recovery_window_deadline"])
    return item, deadline


def _validate_retention(value: object) -> tuple[Mapping[str, object], datetime]:
    item = _mapping(value, _RETENTION_FIELDS)
    if (
        item["schema"] != "exomem.consolidation-source-retention/v1"
        or item["state"] != "required-through-cutover"
        or item["surviving_copy_required"] is not True
    ):
        _fail()
    _integer(item["recovery_window_ttl_ms"], minimum=1)
    _deadline_text, deadline = _timestamp(item["recovery_window_deadline"])
    return item, deadline


def _validate_impact(
    value: object, actions: Sequence[Mapping[str, object]]
) -> Mapping[str, object]:
    item = _mapping(value, _IMPACT_FIELDS)
    if item["schema"] != "exomem.consolidation-impact-summary/v1":
        _fail()
    for field in _IMPACT_FIELDS - {
        "schema",
        "rollback_consequence",
        "surviving_copy_obligation",
    }:
        _integer(item[field])
    _identifier_or_prose(item["rollback_consequence"])
    _identifier_or_prose(item["surviving_copy_obligation"])
    if item["unresolved_count"] != 0:
        _fail()
    expected_counts = {
        "create_count": sum(action["action"] == "add" for action in actions),
        "overwrite_count": sum(action["action"] == "overwrite" for action in actions),
        "removal_count": sum(action["action"] == "remove" for action in actions),
        "relocation_count": sum(
            action["action"] == "relocate_preserving_bytes" for action in actions
        ),
        "deduplication_count": sum(action["action"] == "deduplicate_exact" for action in actions),
        "batch_count": len({action["batch_ordinal"] for action in actions}),
    }
    if any(item[field] != count for field, count in expected_counts.items()):
        _fail()
    return item


def _identifier_or_prose(value: object) -> str:
    text = _normalize_text(value)
    if not text or len(text) > 4096:
        _fail()
    return text


def _render_section_rows(
    value: Mapping[str, object],
) -> tuple[tuple[str, tuple[Mapping[str, object], ...]], ...]:
    impact = _mapping(value["impact_summary"], _IMPACT_FIELDS)
    actions = tuple(_mapping(item, _ACTION_FIELDS) for item in _sequence(value["content_actions"]))
    documents = tuple(
        {
            "row_kind": "policy-document",
            **dict(_mapping(item, _POLICY_DOCUMENT_FIELDS)),
        }
        for item in _sequence(value["policy_documents"], maximum=1024)
    )
    policy_fingerprints: Mapping[str, object] = {
        "row_kind": "policy-fingerprints",
        "policy_bundle_digest": value["policy_bundle_digest"],
        "prospective_policy_fingerprint": value["prospective_policy_fingerprint"],
        "bridge_fingerprints": tuple(_sequence(value["bridge_fingerprints"], maximum=4096)),
        "exact_release_approval_fingerprints": tuple(
            _sequence(value["exact_release_approval_fingerprints"], maximum=4096)
        ),
    }
    principal_disclosure: Mapping[str, object] = {
        "row_kind": "principal-disclosure",
        "principal_attestation_set_digest": value["principal_attestation_set_digest"],
        "disclosure_matrix_digest": value["disclosure_matrix_digest"],
    }
    verification: Mapping[str, object] = {
        "row_kind": "verification",
        **dict(_mapping(value["verification_plan"], _VERIFICATION_FIELDS)),
    }
    rollback_retention: Mapping[str, object] = {
        "row_kind": "rollback-retention",
        "rollback_contingency": dict(_mapping(value["rollback_contingency"], _ROLLBACK_FIELDS)),
        "source_retention": dict(_mapping(value["source_retention"], _RETENTION_FIELDS)),
    }
    return (
        ("impact-summary", (impact,)),
        ("content-actions", actions),
        ("policy", (*documents, policy_fingerprints)),
        ("principals-disclosure", (principal_disclosure,)),
        ("verification", (verification,)),
        ("rollback-retention", (rollback_retention,)),
    )


def _derived_rendering_definition(value: Mapping[str, object]) -> dict[str, object]:
    sections: list[dict[str, object]] = []
    total_rows = 0
    first_page = 0
    for ordinal, (section_id, rows) in enumerate(_render_section_rows(value)):
        row_count = len(rows)
        if row_count == 0:
            _fail()
        page_count = (row_count + RENDER_PAGE_SIZE - 1) // RENDER_PAGE_SIZE
        section_digest = _canonical_object(
            {
                "schema": "exomem.consolidation-render-section/v1",
                "section_id": section_id,
                "rows": rows,
            },
            _RENDER_SECTION_DOMAIN,
        ).digest
        sections.append(
            {
                "ordinal": ordinal,
                "section_id": section_id,
                "row_count": row_count,
                "first_page_ordinal": first_page,
                "page_count": page_count,
                "content_digest": section_digest,
            }
        )
        total_rows += row_count
        first_page += page_count
    return {
        "schema": "exomem.consolidation-rendering-definition/v1",
        "page_size": RENDER_PAGE_SIZE,
        "page_count": first_page,
        "total_rows": total_rows,
        "sections": sections,
    }


def derive_rendering_definition(
    plan_fields: Mapping[str, object],
) -> Mapping[str, object]:
    """Derive the trusted review topology from immutable plan rows."""

    if not isinstance(plan_fields, Mapping):
        _fail()
    fields = frozenset(plan_fields)
    if fields == _BASE_FIELDS:
        source = {key: item for key, item in plan_fields.items() if key != "rendering_definition"}
    elif fields == _BASE_FIELDS - {"rendering_definition"}:
        source = dict(plan_fields)
    else:
        _fail()
    actions = _validate_content_actions(source["content_actions"])
    documents = _validate_policy_documents(source["policy_documents"])
    _validate_impact(source["impact_summary"], actions)
    _validate_verification(source["verification_plan"])
    _validate_rollback(source["rollback_contingency"])
    _validate_retention(source["source_retention"])
    _digest(source["policy_bundle_digest"])
    _digest(source["prospective_policy_fingerprint"])
    source["content_actions"] = actions
    source["policy_documents"] = documents
    source["bridge_fingerprints"] = _sorted_digests(source["bridge_fingerprints"])
    source["exact_release_approval_fingerprints"] = _sorted_digests(
        source["exact_release_approval_fingerprints"]
    )
    _digest(source["principal_attestation_set_digest"])
    _digest(source["disclosure_matrix_digest"])
    return _derived_rendering_definition(source)


def _validate_rendering(value: object) -> Mapping[str, object]:
    item = _mapping(value, _RENDERING_FIELDS)
    if item["schema"] != "exomem.consolidation-rendering-definition/v1":
        _fail()
    if _integer(item["page_size"], minimum=1, maximum=1000) != RENDER_PAGE_SIZE:
        _fail()
    page_count = _integer(item["page_count"], minimum=1, maximum=(1 << 31) - 1)
    total_rows = _integer(item["total_rows"])
    sections = _sequence(item["sections"], maximum=len(_SECTION_IDS))
    if len(sections) != len(_SECTION_IDS):
        _fail()
    next_page = 0
    rows = 0
    for ordinal, (expected_id, raw) in enumerate(zip(_SECTION_IDS, sections, strict=True)):
        section = _mapping(raw, _SECTION_FIELDS)
        if (
            _integer(section["ordinal"]) != ordinal
            or section["section_id"] != expected_id
            or _integer(section["first_page_ordinal"]) != next_page
        ):
            _fail()
        row_count = _integer(section["row_count"])
        section_pages = _integer(section["page_count"], minimum=1)
        _digest(section["content_digest"])
        rows += row_count
        next_page += section_pages
    if next_page != page_count or rows != total_rows:
        _fail()
    return item


def _validate_base(value: Mapping[str, object]) -> dict[str, object]:
    item = _mapping(value, _BASE_FIELDS)
    if item["schema"] != PLAN_SCHEMA or _integer(item["protocol_version"]) != 1:
        _fail()
    plan_kind = _identifier(item["plan_kind"])
    run_mode = _identifier(item["run_mode"])
    if plan_kind not in _PLAN_KINDS or run_mode not in _RUN_MODES:
        _fail()
    _uuid4(item["run_id"])
    for field in (
        "source_snapshot_fingerprint",
        "destination_snapshot_fingerprint",
        "expected_destination_preimage_census_digest",
        "source_inventory_digest",
        "reconciliation_digest",
        "conflict_decision_digest",
        "identity_map_digest",
        "path_map_digest",
        "dependency_map_digest",
        "journal_batch_partition_digest",
        "policy_bundle_digest",
        "prospective_policy_fingerprint",
        "principal_attestation_set_digest",
        "disclosure_matrix_digest",
    ):
        _digest(item[field])
    actions = _validate_content_actions(item["content_actions"])
    _validate_policy_documents(item["policy_documents"])
    bridge_fingerprints = _sorted_digests(item["bridge_fingerprints"])
    release_fingerprints = _sorted_digests(item["exact_release_approval_fingerprints"])
    _validate_verification(item["verification_plan"])
    _rollback, rollback_deadline = _validate_rollback(item["rollback_contingency"])
    _retention, retention_deadline = _validate_retention(item["source_retention"])
    if rollback_deadline != retention_deadline:
        _fail()
    automaton = plan_successor_automaton()
    if item["plan_successor_automaton_digest"] != automaton.digest:
        _fail()
    _validate_impact(item["impact_summary"], actions)
    provided_rendering = _validate_rendering(item["rendering_definition"])
    expected_rendering = derive_rendering_definition(item)
    if canonical_closed_jcs(provided_rendering) != canonical_closed_jcs(expected_rendering):
        _fail()
    created_text, created = _timestamp(item["created_at"])
    valid_text, valid = _timestamp(item["valid_until"])
    if created >= valid or valid > retention_deadline:
        _fail()
    _identifier(item["nonce"])
    normalized = dict(item)
    normalized["bridge_fingerprints"] = bridge_fingerprints
    normalized["exact_release_approval_fingerprints"] = release_fingerprints
    normalized["rendering_definition"] = expected_rendering
    normalized["created_at"] = created_text
    normalized["valid_until"] = valid_text
    return normalized


def _validate_control(value: Mapping[str, object]) -> Mapping[str, object]:
    item = _mapping(value, _CONTROL_FIELDS)
    if item["schema"] != CONTROL_BASIS_SCHEMA:
        _fail()
    _uuid4(item["run_id"])
    if item["plan_kind"] not in _PLAN_KINDS:
        _fail()
    _uuid4(item["plan_materialization_operation_id"])
    _integer(item["basis_run_revision"])
    for field in (
        "source_snapshot_fingerprint",
        "destination_snapshot_fingerprint",
        "plan_input_set_digest",
        "predecessor_event_id",
        "predecessor_payload_digest",
        "plan_successor_automaton_digest",
    ):
        _digest(item[field])
    _identifier(item["plan_nonce"])
    return item


def _build_plan(
    full: Mapping[str, object],
    *,
    control_basis: CanonicalObject,
) -> CanonicalConsolidationPlan:
    normalized_full = _normalize_value(full)
    if not isinstance(normalized_full, Mapping):
        _fail()
    plan = _mapping(normalized_full, _PLAN_FIELDS)
    base = {key: value for key, value in plan.items() if key != "control_basis_digest"}
    normalized_base = _validate_base(base)
    input_set = _canonical_object(normalized_base, _PLAN_INPUT_SET_DOMAIN)
    checked_control = _canonical_object(
        _validate_control(control_basis.preimage), _CONTROL_BASIS_DOMAIN
    )
    if checked_control != control_basis:
        _fail()
    control = checked_control.preimage
    if (
        plan["control_basis_digest"] != control_basis.digest
        or control["run_id"] != normalized_base["run_id"]
        or control["plan_kind"] != normalized_base["plan_kind"]
        or control["source_snapshot_fingerprint"] != normalized_base["source_snapshot_fingerprint"]
        or control["destination_snapshot_fingerprint"]
        != normalized_base["destination_snapshot_fingerprint"]
        or control["plan_input_set_digest"] != input_set.digest
        or control["plan_nonce"] != normalized_base["nonce"]
        or control["plan_successor_automaton_digest"]
        != normalized_base["plan_successor_automaton_digest"]
    ):
        _fail()
    checked_full = dict(normalized_base)
    checked_full["control_basis_digest"] = control_basis.digest
    plan_object = _canonical_object(checked_full, _PLAN_DOMAIN)
    impact = _canonical_object(
        _mapping(checked_full["impact_summary"], _IMPACT_FIELDS),
        _IMPACT_SUMMARY_DOMAIN,
    )
    rendering = _canonical_object(
        _mapping(checked_full["rendering_definition"], _RENDERING_FIELDS),
        _RENDERING_DEFINITION_DOMAIN,
    )
    return CanonicalConsolidationPlan(
        preimage=plan_object.preimage,
        canonical_bytes=plan_object.canonical_bytes,
        framed_bytes=plan_object.framed_bytes,
        digest=plan_object.digest,
        plan_input_set_digest=input_set.digest,
        control_basis=control_basis,
        impact_summary_digest=impact.digest,
        rendering_definition_digest=rendering.digest,
    )


def plan_successor_automaton() -> CanonicalObject:
    """Return the protocol-static review automaton and its framed digest."""

    transitions = (
        (0, "plan-materialized", "render-begin", "once"),
        (1, "render-begin", "render-page", "page-0"),
        (2, "render-page", "render-ack", "same-page"),
        (3, "render-ack", "render-page", "next-page-if-any"),
        (4, "render-ack", "render-complete", "last-page"),
        (5, "render-complete", "approval", "complete-coverage"),
        (6, "approval", "token-reservation", "matching-kind-token"),
    )
    value = {
        "schema": PLAN_SUCCESSOR_AUTOMATON_SCHEMA,
        "initial_state": "plan-materialized",
        "states": (
            "plan-materialized",
            "render-begin",
            "render-page",
            "render-ack",
            "render-complete",
            "approval",
            "token-reservation",
        ),
        "terminal_state": "token-reservation",
        "minimum_pages": 1,
        "page_count_source": "stored-plan-rendering-definition",
        "transitions": tuple(
            {
                "ordinal": ordinal,
                "from_state": from_state,
                "to_state": to_state,
                "guard": guard,
            }
            for ordinal, from_state, to_state, guard in transitions
        ),
        "retry_rule": "adopt-existing-identical-event",
        "unexpected_event_rule": "stale-plan",
    }
    return _canonical_object(value, _AUTOMATON_DOMAIN)


def materialize_plan(
    preimage_without_control_basis: Mapping[str, object],
    *,
    materialization: PlanMaterializationContext,
) -> CanonicalConsolidationPlan:
    """Materialize one plan and its immutable semantic control ancestor."""

    normalized = _normalize_value(preimage_without_control_basis)
    if not isinstance(normalized, Mapping):
        _fail()
    base = _validate_base(normalized)
    if not isinstance(materialization, PlanMaterializationContext):
        _fail()
    operation_id = _uuid4(materialization.operation_id)
    revision = _integer(materialization.basis_run_revision)
    predecessor_event_id = _digest(materialization.predecessor_event_id)
    predecessor_payload_digest = _digest(materialization.predecessor_payload_digest)
    input_set = _canonical_object(base, _PLAN_INPUT_SET_DOMAIN)
    control = _canonical_object(
        {
            "schema": CONTROL_BASIS_SCHEMA,
            "run_id": base["run_id"],
            "plan_kind": base["plan_kind"],
            "plan_materialization_operation_id": operation_id,
            "basis_run_revision": revision,
            "source_snapshot_fingerprint": base["source_snapshot_fingerprint"],
            "destination_snapshot_fingerprint": base["destination_snapshot_fingerprint"],
            "plan_input_set_digest": input_set.digest,
            "plan_nonce": base["nonce"],
            "predecessor_event_id": predecessor_event_id,
            "predecessor_payload_digest": predecessor_payload_digest,
            "plan_successor_automaton_digest": base["plan_successor_automaton_digest"],
        },
        _CONTROL_BASIS_DOMAIN,
    )
    full = dict(base)
    full["control_basis_digest"] = control.digest
    return _build_plan(full, control_basis=control)


def render_plan_page(
    plan: CanonicalConsolidationPlan,
    *,
    page_ordinal: int,
) -> CanonicalPlanPage:
    """Derive one bounded trusted-human page from stored canonical plan bytes."""

    if not isinstance(plan, CanonicalConsolidationPlan):
        _fail()
    checked = _build_plan(plan.preimage, control_basis=plan.control_basis)
    if checked != plan:
        _fail()
    ordinal = _integer(page_ordinal)
    definition = _mapping(plan.preimage["rendering_definition"], _RENDERING_FIELDS)
    total_pages = _integer(definition["page_count"], minimum=1)
    total_rows = _integer(definition["total_rows"], minimum=1)
    if ordinal >= total_pages:
        _fail()
    sections = _render_section_rows(plan.preimage)
    selected_section_id = ""
    selected_rows: tuple[Mapping[str, object], ...] = ()
    selected_section_page = -1
    selected_row_start = -1
    for section_id, rows in sections:
        section_pages = (len(rows) + RENDER_PAGE_SIZE - 1) // RENDER_PAGE_SIZE
        if ordinal < section_pages:
            selected_section_id = section_id
            selected_rows = rows
            selected_section_page = ordinal
            selected_row_start = ordinal * RENDER_PAGE_SIZE
            break
        ordinal -= section_pages
    if selected_section_page < 0:
        _fail()
    selected_row_stop = min(selected_row_start + RENDER_PAGE_SIZE, len(selected_rows))
    page_rows = selected_rows[selected_row_start:selected_row_stop]
    impact = _mapping(plan.preimage["impact_summary"], _IMPACT_FIELDS)
    page_preimage = {
        "schema": "exomem.consolidation-render-page/v1",
        "run_id": plan.preimage["run_id"],
        "plan_kind": plan.preimage["plan_kind"],
        "plan_digest": plan.digest,
        "page_ordinal": page_ordinal,
        "section_id": selected_section_id,
        "section_page_ordinal": selected_section_page,
        "section_row_count": len(selected_rows),
        "page_row_start": selected_row_start,
        "page_row_stop": selected_row_stop,
        "total_pages": total_pages,
        "total_rows": total_rows,
        "impact_summary": impact,
        "rows": page_rows,
    }
    page = _canonical_object(page_preimage, _RENDER_PAGE_DOMAIN)
    frozen_rows = _freeze(page_rows)
    frozen_impact = _freeze(impact)
    if not isinstance(frozen_rows, tuple) or not isinstance(frozen_impact, Mapping):
        _fail()
    return CanonicalPlanPage(
        plan_digest=plan.digest,
        plan_kind=_identifier(plan.preimage["plan_kind"]),
        run_id=_uuid4(plan.preimage["run_id"]),
        page_ordinal=page_ordinal,
        section_id=selected_section_id,
        section_page_ordinal=selected_section_page,
        total_pages=total_pages,
        total_rows=total_rows,
        section_row_count=len(selected_rows),
        page_row_start=selected_row_start,
        page_row_stop=selected_row_stop,
        impact_summary=frozen_impact,
        rows=frozen_rows,
        canonical_bytes=page.canonical_bytes,
        digest=page.digest,
    )


def _reject_constant(_value: str) -> NoReturn:
    _fail()


def _reject_float(_value: str) -> NoReturn:
    _fail()


def _parse_integer(value: str) -> int:
    if value.startswith("-"):
        _fail()
    try:
        parsed = int(value)
    except ValueError:
        _fail()
    return _integer(parsed)


def _closed_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for raw_key, item in pairs:
        key = _normalize_text(raw_key)
        if key in value:
            _fail()
        value[key] = item
    return value


def _parse_canonical_mapping(raw: bytes, *, maximum: int) -> Mapping[str, object]:
    if not isinstance(raw, bytes) or len(raw) > maximum:
        _fail()
    try:
        parsed = json.loads(
            raw,
            object_pairs_hook=_closed_object,
            parse_int=_parse_integer,
            parse_float=_reject_float,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ConsolidationPlanUnavailable):
        _fail()
    if not isinstance(parsed, Mapping):
        _fail()
    return parsed


def parse_control_basis(raw: bytes) -> CanonicalObject:
    """Parse exact stored control-basis bytes and recompute their framed digest."""

    parsed = _parse_canonical_mapping(raw, maximum=64 * 1024)
    control = _canonical_object(_validate_control(parsed), _CONTROL_BASIS_DOMAIN)
    if control.canonical_bytes != raw:
        _fail()
    return control


def parse_journal_batch_partition(raw: bytes) -> CanonicalObject:
    """Parse one exact canonical batch partition and recompute its framed digest."""

    parsed = _parse_canonical_mapping(raw, maximum=16 * 1024 * 1024)
    if frozenset(parsed) != {"schema", "action_count", "batch_count", "batches"}:
        _fail()
    if parsed["schema"] != JOURNAL_BATCH_PARTITION_SCHEMA:
        _fail()
    action_count = _integer(parsed["action_count"], minimum=1)
    batch_count = _integer(parsed["batch_count"], minimum=1)
    batches = _sequence(parsed["batches"])
    fields = frozenset(
        {
            "batch_ordinal",
            "first_action_ordinal",
            "last_action_ordinal",
            "action_count",
            "publication_boundary",
            "action_set_digest",
            "prior_fingerprint",
            "prepared_fingerprint",
            "final_fingerprint",
        }
    )
    if len(batches) != batch_count:
        _fail()
    seen_actions = 0
    for ordinal, raw_batch in enumerate(batches):
        batch = _mapping(raw_batch, fields)
        count = _integer(
            batch["action_count"],
            minimum=1,
            maximum=MAX_CONTENT_BATCH_ACTIONS,
        )
        first = _integer(batch["first_action_ordinal"])
        last = _integer(batch["last_action_ordinal"])
        if (
            _integer(batch["batch_ordinal"]) != ordinal
            or batch["publication_boundary"] is not (ordinal == 0)
            or first != seen_actions
            or last != first + count - 1
        ):
            _fail()
        for field in (
            "action_set_digest",
            "prior_fingerprint",
            "prepared_fingerprint",
            "final_fingerprint",
        ):
            _digest(batch[field])
        if (
            len(
                {
                    batch["prior_fingerprint"],
                    batch["prepared_fingerprint"],
                    batch["final_fingerprint"],
                }
            )
            != 3
        ):
            _fail()
        seen_actions += count
    if seen_actions != action_count:
        _fail()
    result = _canonical_object(parsed, _JOURNAL_BATCH_PARTITION_DOMAIN)
    if result.canonical_bytes != raw:
        _fail()
    return result


def parse_canonical_plan(
    raw: bytes,
    *,
    control_basis: CanonicalObject,
) -> CanonicalConsolidationPlan:
    """Parse only exact canonical stored bytes and rebind their control basis."""

    parsed = _parse_canonical_mapping(raw, maximum=16 * 1024 * 1024)
    plan = _build_plan(parsed, control_basis=control_basis)
    if plan.canonical_bytes != raw:
        _fail()
    return plan
