"""Durable exact policy-publication inputs for consolidation recovery."""

from __future__ import annotations

import base64
import hashlib
import json
import re
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import NoReturn

from .. import reserved_paths
from ..kbdir import kb_dirname
from . import (
    consolidation_admission,
    consolidation_apply_coordinator,
    consolidation_authority,
    consolidation_effect_coordinator,
    consolidation_plan,
    consolidation_plan_store,
    consolidation_policy,
    consolidation_receipts,
    consolidation_saga,
    consolidation_seal,
    policy,
    policy_publication,
    receipts,
    schema_v4,
)
from .principal import RequestPrincipal

PUBLICATION_RECORD_SCHEMA = "exomem.consolidation-policy-publication/v1"

_BINDING_SCHEMA = "exomem.consolidation-policy-publication-binding/v1"
_BINDING_DOMAIN = _BINDING_SCHEMA.encode("ascii")
_DESCRIPTOR_ID = "consolidation-tree"
_OWNER = "consolidation.run"
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_COMMITTED_EVENT = re.compile(r"[0-9a-f]{64}:committed\Z")
_UUID4 = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z"
)
_MAX_RECORD_BYTES = 16 * 1024 * 1024
_MAX_SAFE_INTEGER = (1 << 53) - 1
_CROCKFORD32 = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_IDENTITY_SCHEMA = "exomem.consolidation-policy-publication-identity/v1"
_IDENTITY_DOMAIN = _IDENTITY_SCHEMA.encode("ascii")
_RFC3339_MILLISECONDS = re.compile(
    r"[0-9]{4}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12][0-9]|3[01])"
    r"T(?:[01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]\.[0-9]{3}Z\Z"
)
_RECORD_FIELDS = frozenset(
    {
        "schema",
        "run_id",
        "operation_id",
        "effect_ordinal",
        "request_digest",
        "plan_digest",
        "policy_bundle_digest",
        "preimage_terminal_event_id",
        "preimage_terminal_payload_digest",
        "binding_digest",
        "publication",
    }
)

__all__ = [
    "PUBLICATION_RECORD_SCHEMA",
    "ConsolidationPolicyActivationUnavailable",
    "ConsolidationPolicyPublicationIdentity",
    "ConsolidationPolicyPublicationRecord",
    "ConsolidationPolicyPublicationStore",
    "ConsolidationPolicyActivationResult",
    "activate_stored_destination_policy",
    "derive_policy_publication_identity",
]


class ConsolidationPolicyActivationUnavailable(RuntimeError):
    """Content-free refusal for missing, changed, or ambiguous activation state."""

    code = "CONSOLIDATION_POLICY_ACTIVATION_UNAVAILABLE"

    def __init__(self) -> None:
        super().__init__(self.code)


@dataclass(frozen=True, slots=True)
class ConsolidationPolicyActivationResult:
    """Receipt-proven policy activation, before any content publication."""

    policy_prepare: consolidation_effect_coordinator.EffectExecutionResult
    policy_active: consolidation_effect_coordinator.EffectExecutionResult
    terminal: consolidation_saga.PolicyActivationTerminal
    seal_state: consolidation_seal.ConsolidationSealState


@dataclass(frozen=True, slots=True)
class ConsolidationPolicyPublicationRecord:
    schema: str
    run_id: str
    operation_id: str
    effect_ordinal: int
    request_digest: str
    plan_digest: str
    policy_bundle_digest: str
    preimage_terminal_event_id: str
    preimage_terminal_payload_digest: str
    binding_digest: str
    publication: policy_publication.PreparedPolicyPublication
    state_digest: str


@dataclass(frozen=True, slots=True)
class ConsolidationPolicyPublicationIdentity:
    generation_id: str
    authoring_event_id: str
    receipt_event_id: str
    binding_digest: str


def _fail() -> NoReturn:
    raise ConsolidationPolicyActivationUnavailable from None


def _digest(value: object) -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        _fail()
    return value


def _uuid4(value: object) -> str:
    if not isinstance(value, str) or _UUID4.fullmatch(value) is None:
        _fail()
    return value


def _integer(value: object, *, minimum: int = 0) -> int:
    if type(value) is not int or not minimum <= value <= _MAX_SAFE_INTEGER:
        _fail()
    return value


def _text(value: object) -> str:
    if not isinstance(value, str) or not value:
        _fail()
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        _fail()
    return value


def _timestamp(value: object) -> tuple[str, datetime]:
    checked = _text(value)
    if _RFC3339_MILLISECONDS.fullmatch(checked) is None:
        _fail()
    try:
        parsed = datetime.fromisoformat(checked.removesuffix("Z") + "+00:00")
    except ValueError:
        _fail()
    if parsed.tzinfo != UTC:
        _fail()
    return checked, parsed


def derive_policy_publication_identity(
    *,
    destination_vault_id: str,
    vault_binding_digest: str,
    run_id: str,
    operation_id: str,
    request_digest: str,
    plan_digest: str,
    policy_bundle_digest: str,
    preimage_terminal_event_id: str,
    preimage_terminal_payload_digest: str,
    policy_prepared_at: str,
) -> ConsolidationPolicyPublicationIdentity:
    """Derive stable publication identities without proposal-table semantics."""

    prepared_at, parsed = _timestamp(policy_prepared_at)
    if (
        not isinstance(preimage_terminal_event_id, str)
        or _COMMITTED_EVENT.fullmatch(preimage_terminal_event_id) is None
    ):
        _fail()
    value = {
        "schema": _IDENTITY_SCHEMA,
        "destination_vault_id": _text(destination_vault_id),
        "vault_binding_digest": _digest(vault_binding_digest),
        "run_id": _uuid4(run_id),
        "operation_id": _uuid4(operation_id),
        "request_digest": _digest(request_digest),
        "plan_digest": _digest(plan_digest),
        "policy_bundle_digest": _digest(policy_bundle_digest),
        "preimage_terminal_event_id": preimage_terminal_event_id,
        "preimage_terminal_payload_digest": _digest(
            preimage_terminal_payload_digest
        ),
        "policy_prepared_at": prepared_at,
    }
    try:
        raw = consolidation_plan.canonical_closed_jcs(value)
    except consolidation_plan.ConsolidationPlanUnavailable:
        _fail()
    framed = (
        len(_IDENTITY_DOMAIN).to_bytes(4, "big")
        + _IDENTITY_DOMAIN
        + len(raw).to_bytes(8, "big")
        + raw
    )
    binding_digest = hashlib.sha256(framed).hexdigest()
    milliseconds = int(parsed.timestamp() * 1000)
    if not 0 <= milliseconds < (1 << 48):
        _fail()
    entropy = hashlib.sha256(_IDENTITY_DOMAIN + b"\0" + raw).digest()[:10]
    ulid = (milliseconds << 80) | int.from_bytes(entropy, "big")
    generation_id = "".join(
        _CROCKFORD32[(ulid >> shift) & 31] for shift in range(125, -1, -5)
    )
    authoring_event_id = receipts.critical_event_id(
        {
            "operation": "consolidation_policy_authoring_review",
            "binding_digest": binding_digest,
            "generation_id": generation_id,
        }
    )
    receipt_event_id = receipts.critical_event_id(
        {
            "operation": "governance_policy_publication",
            "authoring_event_id": authoring_event_id,
            "binding_digest": binding_digest,
            "generation_id": generation_id,
        }
    )
    return ConsolidationPolicyPublicationIdentity(
        generation_id=generation_id,
        authoring_event_id=authoring_event_id,
        receipt_event_id=receipt_event_id,
        binding_digest=binding_digest,
    )


def _mapping(value: object, fields: frozenset[str]) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or frozenset(value) != fields:
        _fail()
    return value


def _encode_bytes(value: bytes) -> str:
    if not isinstance(value, bytes):
        _fail()
    return base64.b64encode(value).decode("ascii")


def _decode_bytes(value: object) -> bytes:
    if not isinstance(value, str):
        _fail()
    try:
        return base64.b64decode(value, validate=True)
    except (TypeError, ValueError):
        _fail()


def _expected_value(value: schema_v4.VerifiedActiveGovernanceState) -> dict[str, object]:
    if not isinstance(value, schema_v4.VerifiedActiveGovernanceState):
        _fail()
    return {
        "logical_vault_id": _text(value.logical_vault_id),
        "activation_store_id": _text(value.activation_store_id),
        "activation_epoch": _integer(value.activation_epoch, minimum=1),
        "activation_state_digest": _digest(value.activation_state_digest),
        "policy_generation_id": _text(value.policy_generation_id),
        "policy_fingerprint": _digest(value.policy_fingerprint),
        "projector_schema_version": _integer(
            value.projector_schema_version,
            minimum=1,
        ),
        "catalog_generation": _integer(value.catalog_generation, minimum=1),
        "projection_namespace_id": _text(value.projection_namespace_id),
    }


def _expected_from_value(value: object) -> schema_v4.VerifiedActiveGovernanceState:
    item = _mapping(
        value,
        frozenset(
            {
                "logical_vault_id",
                "activation_store_id",
                "activation_epoch",
                "activation_state_digest",
                "policy_generation_id",
                "policy_fingerprint",
                "projector_schema_version",
                "catalog_generation",
                "projection_namespace_id",
            }
        ),
    )
    return schema_v4.VerifiedActiveGovernanceState(
        logical_vault_id=_text(item["logical_vault_id"]),
        activation_store_id=_text(item["activation_store_id"]),
        activation_epoch=_integer(item["activation_epoch"], minimum=1),
        activation_state_digest=_digest(item["activation_state_digest"]),
        policy_generation_id=_text(item["policy_generation_id"]),
        policy_fingerprint=_digest(item["policy_fingerprint"]),
        projector_schema_version=_integer(
            item["projector_schema_version"],
            minimum=1,
        ),
        catalog_generation=_integer(item["catalog_generation"], minimum=1),
        projection_namespace_id=_text(item["projection_namespace_id"]),
    )


def _policy_value(value: schema_v4.PolicyGenerationSeed) -> dict[str, object]:
    if not isinstance(value, schema_v4.PolicyGenerationSeed):
        _fail()
    documents = [
        {"path": _text(path), "bytes": _encode_bytes(raw)}
        for path, raw in value.source_documents
    ]
    if tuple(row["path"] for row in documents) != tuple(
        sorted({str(row["path"]) for row in documents})
    ):
        _fail()
    compiled = policy.compile_documents(dict(value.source_documents))
    if (
        compiled.empty
        or compiled.blocked
        or compiled.conflicted
        or value.source_fingerprint != compiled.fingerprint
        or value.policy_fingerprint != compiled.fingerprint
        or value.compiled_policy != policy.canonical_compiled_bytes(compiled)
    ):
        _fail()
    return {
        "generation_id": _text(value.generation_id),
        "source_documents": documents,
        "source_fingerprint": _digest(value.source_fingerprint),
        "conflict_digest": _digest(value.conflict_digest),
        "compiled_policy": _encode_bytes(value.compiled_policy),
        "policy_fingerprint": _digest(value.policy_fingerprint),
        "compiler_schema_version": _integer(
            value.compiler_schema_version,
            minimum=1,
        ),
        "projector_schema_version": _integer(
            value.projector_schema_version,
            minimum=1,
        ),
        "predecessor_generation": (
            {"state": "absent"}
            if value.predecessor_generation_id is None
            else {
                "state": "present",
                "generation_id": _text(value.predecessor_generation_id),
            }
        ),
        "authoring_event_id": _text(value.authoring_event_id),
        "receipt_event_id": _digest(value.receipt_event_id),
        "created_at": _integer(value.created_at),
    }


def _policy_from_value(value: object) -> schema_v4.PolicyGenerationSeed:
    item = _mapping(
        value,
        frozenset(
            {
                "generation_id",
                "source_documents",
                "source_fingerprint",
                "conflict_digest",
                "compiled_policy",
                "policy_fingerprint",
                "compiler_schema_version",
                "projector_schema_version",
                "predecessor_generation",
                "authoring_event_id",
                "receipt_event_id",
                "created_at",
            }
        ),
    )
    raw_documents = item["source_documents"]
    if isinstance(raw_documents, (str, bytes)) or not isinstance(
        raw_documents,
        Sequence,
    ):
        _fail()
    documents = tuple(
        (
            _text(row["path"]),
            _decode_bytes(row["bytes"]),
        )
        for raw in raw_documents
        for row in [_mapping(raw, frozenset({"path", "bytes"}))]
    )
    predecessor = item["predecessor_generation"]
    if predecessor == {"state": "absent"}:
        predecessor_generation_id = None
    else:
        predecessor_row = _mapping(
            predecessor,
            frozenset({"state", "generation_id"}),
        )
        if predecessor_row["state"] != "present":
            _fail()
        predecessor_generation_id = _text(predecessor_row["generation_id"])
    seed = schema_v4.PolicyGenerationSeed(
        generation_id=_text(item["generation_id"]),
        source_documents=documents,
        source_fingerprint=_digest(item["source_fingerprint"]),
        conflict_digest=_digest(item["conflict_digest"]),
        compiled_policy=_decode_bytes(item["compiled_policy"]),
        policy_fingerprint=_digest(item["policy_fingerprint"]),
        compiler_schema_version=_integer(
            item["compiler_schema_version"],
            minimum=1,
        ),
        projector_schema_version=_integer(
            item["projector_schema_version"],
            minimum=1,
        ),
        predecessor_generation_id=predecessor_generation_id,
        authoring_event_id=_text(item["authoring_event_id"]),
        receipt_event_id=_digest(item["receipt_event_id"]),
        created_at=_integer(item["created_at"]),
    )
    _policy_value(seed)
    return seed


def _catalog_value(value: schema_v4.CatalogGenerationSeed | None) -> object:
    if value is None:
        return {"state": "absent"}
    if not isinstance(value, schema_v4.CatalogGenerationSeed):
        _fail()
    return {
        "state": "present",
        "catalog_generation": _integer(value.catalog_generation, minimum=1),
        "descriptor": _encode_bytes(value.descriptor),
        "artifact_count": _integer(value.artifact_count),
        "created_at": _integer(value.created_at),
    }


def _catalog_from_value(value: object) -> schema_v4.CatalogGenerationSeed | None:
    if value == {"state": "absent"}:
        return None
    item = _mapping(
        value,
        frozenset(
            {
                "state",
                "catalog_generation",
                "descriptor",
                "artifact_count",
                "created_at",
            }
        ),
    )
    if item["state"] != "present":
        _fail()
    return schema_v4.CatalogGenerationSeed(
        catalog_generation=_integer(item["catalog_generation"], minimum=1),
        descriptor=_decode_bytes(item["descriptor"]),
        artifact_count=_integer(item["artifact_count"]),
        created_at=_integer(item["created_at"]),
    )


def _namespace_value(value: schema_v4.ProjectionNamespaceSeed) -> dict[str, object]:
    if not isinstance(value, schema_v4.ProjectionNamespaceSeed):
        _fail()
    return {
        "namespace_id": _text(value.namespace_id),
        "evidence": _encode_bytes(value.evidence),
        "ready_at": _integer(value.ready_at),
    }


def _namespace_from_value(value: object) -> schema_v4.ProjectionNamespaceSeed:
    item = _mapping(value, frozenset({"namespace_id", "evidence", "ready_at"}))
    return schema_v4.ProjectionNamespaceSeed(
        namespace_id=_text(item["namespace_id"]),
        evidence=_decode_bytes(item["evidence"]),
        ready_at=_integer(item["ready_at"]),
    )


def _grant_value(value: schema_v4.DependentGrantTransition) -> dict[str, object]:
    if not isinstance(value, schema_v4.DependentGrantTransition):
        _fail()
    if value.target_status not in {"review", "expired"}:
        _fail()
    return {
        "grant_id": _text(value.grant_id),
        "expected_policy_fingerprint": _digest(value.expected_policy_fingerprint),
        "expected_membership_manifest": _text(value.expected_membership_manifest),
        "target_status": value.target_status,
        "target_policy_fingerprint": _digest(value.target_policy_fingerprint),
        "target_membership_manifest": _text(value.target_membership_manifest),
    }


def _grant_from_value(value: object) -> schema_v4.DependentGrantTransition:
    item = _mapping(
        value,
        frozenset(
            {
                "grant_id",
                "expected_policy_fingerprint",
                "expected_membership_manifest",
                "target_status",
                "target_policy_fingerprint",
                "target_membership_manifest",
            }
        ),
    )
    transition = schema_v4.DependentGrantTransition(
        grant_id=_text(item["grant_id"]),
        expected_policy_fingerprint=_digest(item["expected_policy_fingerprint"]),
        expected_membership_manifest=_text(item["expected_membership_manifest"]),
        target_status=_text(item["target_status"]),
        target_policy_fingerprint=_digest(item["target_policy_fingerprint"]),
        target_membership_manifest=_text(item["target_membership_manifest"]),
    )
    _grant_value(transition)
    return transition


def _publication_value(
    value: policy_publication.PreparedPolicyPublication,
) -> dict[str, object]:
    if not isinstance(value, policy_publication.PreparedPolicyPublication):
        _fail()
    grants = value.dependent_grants
    grant_rows: object = (
        {"state": "legacy-unbound"}
        if grants is None
        else {
            "state": "bound",
            "items": [_grant_value(item) for item in grants],
        }
    )
    if grants is not None and tuple(item.grant_id for item in grants) != tuple(
        sorted({item.grant_id for item in grants})
    ):
        _fail()
    if (
        value.identity.receipt_event_id != value.policy.receipt_event_id
        or value.identity.policy_generation_id != value.policy.generation_id
        or value.expected.policy_generation_id
        != value.policy.predecessor_generation_id
        or value.expected.projector_schema_version
        != value.policy.projector_schema_version
        or any(
            item.expected_policy_fingerprint != value.expected.policy_fingerprint
            or item.target_policy_fingerprint != value.policy.policy_fingerprint
            for item in grants or ()
        )
    ):
        _fail()
    return {
        "identity": {
            "receipt_event_id": _digest(value.identity.receipt_event_id),
            "policy_generation_id": _text(value.identity.policy_generation_id),
        },
        "expected": _expected_value(value.expected),
        "policy": _policy_value(value.policy),
        "catalog": _catalog_value(value.catalog),
        "namespace": _namespace_value(value.namespace),
        "dependent_grants": grant_rows,
    }


def _publication_from_value(
    value: object,
) -> policy_publication.PreparedPolicyPublication:
    item = _mapping(
        value,
        frozenset(
            {
                "identity",
                "expected",
                "policy",
                "catalog",
                "namespace",
                "dependent_grants",
            }
        ),
    )
    identity = _mapping(
        item["identity"],
        frozenset({"receipt_event_id", "policy_generation_id"}),
    )
    raw_grants = item["dependent_grants"]
    grants: tuple[schema_v4.DependentGrantTransition, ...] | None
    if raw_grants == {"state": "legacy-unbound"}:
        grants = None
    else:
        grant_set = _mapping(raw_grants, frozenset({"state", "items"}))
        raw_items = grant_set["items"]
        if (
            grant_set["state"] != "bound"
            or isinstance(raw_items, (str, bytes))
            or not isinstance(raw_items, Sequence)
        ):
            _fail()
        grants = tuple(_grant_from_value(raw) for raw in raw_items)
    publication = policy_publication.prepare_policy_publication(
        expected=_expected_from_value(item["expected"]),
        policy=_policy_from_value(item["policy"]),
        catalog=_catalog_from_value(item["catalog"]),
        namespace=_namespace_from_value(item["namespace"]),
        dependent_grants=grants,
    )
    if (
        publication.identity.receipt_event_id
        != _digest(identity["receipt_event_id"])
        or publication.identity.policy_generation_id
        != _text(identity["policy_generation_id"])
        or _publication_value(publication) != value
    ):
        _fail()
    return publication


def _binding_digest(
    *,
    run_id: str,
    operation_id: str,
    effect_ordinal: int,
    request_digest: str,
    plan_digest: str,
    policy_bundle_digest: str,
    preimage_terminal_event_id: str,
    preimage_terminal_payload_digest: str,
    publication: policy_publication.PreparedPolicyPublication,
) -> str:
    value = {
        "schema": _BINDING_SCHEMA,
        "run_id": _uuid4(run_id),
        "operation_id": _uuid4(operation_id),
        "effect_ordinal": _integer(effect_ordinal),
        "request_digest": _digest(request_digest),
        "plan_digest": _digest(plan_digest),
        "policy_bundle_digest": _digest(policy_bundle_digest),
        "preimage_terminal_event_id": (
            preimage_terminal_event_id
            if isinstance(preimage_terminal_event_id, str)
            and _COMMITTED_EVENT.fullmatch(preimage_terminal_event_id) is not None
            else _fail()
        ),
        "preimage_terminal_payload_digest": _digest(
            preimage_terminal_payload_digest
        ),
        "publication": _publication_value(publication),
    }
    raw = consolidation_plan.canonical_closed_jcs(value)
    framed = (
        len(_BINDING_DOMAIN).to_bytes(4, "big")
        + _BINDING_DOMAIN
        + len(raw).to_bytes(8, "big")
        + raw
    )
    return hashlib.sha256(framed).hexdigest()


def _record_value(record: ConsolidationPolicyPublicationRecord) -> dict[str, object]:
    return {
        "schema": record.schema,
        "run_id": record.run_id,
        "operation_id": record.operation_id,
        "effect_ordinal": record.effect_ordinal,
        "request_digest": record.request_digest,
        "plan_digest": record.plan_digest,
        "policy_bundle_digest": record.policy_bundle_digest,
        "preimage_terminal_event_id": record.preimage_terminal_event_id,
        "preimage_terminal_payload_digest": record.preimage_terminal_payload_digest,
        "binding_digest": record.binding_digest,
        "publication": _publication_value(record.publication),
    }


def _record_bytes(record: ConsolidationPolicyPublicationRecord) -> bytes:
    try:
        return consolidation_plan.canonical_closed_jcs(_record_value(record))
    except consolidation_plan.ConsolidationPlanUnavailable:
        _fail()


def _pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            _fail()
        value[key] = item
    return value


def _reject_number(_value: str) -> NoReturn:
    _fail()


def _parse_integer(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError:
        _fail()
    return _integer(parsed)


def _parse_record(raw: bytes) -> ConsolidationPolicyPublicationRecord:
    if not isinstance(raw, bytes) or not raw or len(raw) > _MAX_RECORD_BYTES:
        _fail()
    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_pairs,
            parse_int=_parse_integer,
            parse_float=_reject_number,
            parse_constant=_reject_number,
        )
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        RecursionError,
        ConsolidationPolicyActivationUnavailable,
    ):
        _fail()
    item = _mapping(value, _RECORD_FIELDS)
    if item["schema"] != PUBLICATION_RECORD_SCHEMA:
        _fail()
    publication = _publication_from_value(item["publication"])
    event_id = item["preimage_terminal_event_id"]
    if not isinstance(event_id, str) or _COMMITTED_EVENT.fullmatch(event_id) is None:
        _fail()
    record = ConsolidationPolicyPublicationRecord(
        schema=PUBLICATION_RECORD_SCHEMA,
        run_id=_uuid4(item["run_id"]),
        operation_id=_uuid4(item["operation_id"]),
        effect_ordinal=_integer(item["effect_ordinal"]),
        request_digest=_digest(item["request_digest"]),
        plan_digest=_digest(item["plan_digest"]),
        policy_bundle_digest=_digest(item["policy_bundle_digest"]),
        preimage_terminal_event_id=event_id,
        preimage_terminal_payload_digest=_digest(
            item["preimage_terminal_payload_digest"]
        ),
        binding_digest=_digest(item["binding_digest"]),
        publication=publication,
        state_digest=hashlib.sha256(raw).hexdigest(),
    )
    expected_binding = _binding_digest(
        run_id=record.run_id,
        operation_id=record.operation_id,
        effect_ordinal=record.effect_ordinal,
        request_digest=record.request_digest,
        plan_digest=record.plan_digest,
        policy_bundle_digest=record.policy_bundle_digest,
        preimage_terminal_event_id=record.preimage_terminal_event_id,
        preimage_terminal_payload_digest=record.preimage_terminal_payload_digest,
        publication=record.publication,
    )
    if record.binding_digest != expected_binding or _record_bytes(record) != raw:
        _fail()
    return record


@contextmanager
def _authority(vault_root: Path, *, mutation: bool) -> Iterator[None]:
    try:
        with reserved_paths._subsystem_authority_scope(_OWNER):  # noqa: SLF001
            with reserved_paths._identity_coordination_scope(  # noqa: SLF001
                vault_root,
                descriptor_ids=(_DESCRIPTOR_ID,),
                identity_may_change=mutation,
            ):
                yield
    except ConsolidationPolicyActivationUnavailable:
        raise
    except (OSError, RuntimeError, ValueError):
        _fail()


class ConsolidationPolicyPublicationStore:
    """Persist the exact non-recomputable inputs needed after policy CAS."""

    def __init__(
        self,
        vault_root: Path | str,
        *,
        run_id: str,
        effect_ordinal: int,
    ) -> None:
        self.vault_root = Path(vault_root).absolute()
        self.run_id = _uuid4(run_id)
        self.effect_ordinal = _integer(effect_ordinal)
        self.path = (
            self.vault_root
            / kb_dirname()
            / "_Consolidation"
            / "runs"
            / self.run_id
            / "effects"
            / f"policy-publication-{self.effect_ordinal:010d}.json"
        )

    def _read_optional(self) -> bytes | None:
        try:
            return reserved_paths._read_owner_bytes(  # noqa: SLF001
                self.vault_root,
                self.path,
                _DESCRIPTOR_ID,
                limit=_MAX_RECORD_BYTES,
            )
        except FileNotFoundError:
            return None

    def load(self) -> ConsolidationPolicyPublicationRecord:
        with _authority(self.vault_root, mutation=False):
            raw = self._read_optional()
            if raw is None:
                _fail()
            record = _parse_record(raw)
            if (
                record.run_id != self.run_id
                or record.effect_ordinal != self.effect_ordinal
            ):
                _fail()
            return record

    def load_optional(self) -> ConsolidationPolicyPublicationRecord | None:
        with _authority(self.vault_root, mutation=False):
            raw = self._read_optional()
            if raw is None:
                return None
            record = _parse_record(raw)
            if (
                record.run_id != self.run_id
                or record.effect_ordinal != self.effect_ordinal
            ):
                _fail()
            return record

    def create(
        self,
        *,
        operation_id: str,
        request_digest: str,
        plan_digest: str,
        policy_bundle_digest: str,
        preimage_terminal_event_id: str,
        preimage_terminal_payload_digest: str,
        publication: policy_publication.PreparedPolicyPublication,
    ) -> ConsolidationPolicyPublicationRecord:
        binding = _binding_digest(
            run_id=self.run_id,
            operation_id=operation_id,
            effect_ordinal=self.effect_ordinal,
            request_digest=request_digest,
            plan_digest=plan_digest,
            policy_bundle_digest=policy_bundle_digest,
            preimage_terminal_event_id=preimage_terminal_event_id,
            preimage_terminal_payload_digest=preimage_terminal_payload_digest,
            publication=publication,
        )
        candidate = ConsolidationPolicyPublicationRecord(
            schema=PUBLICATION_RECORD_SCHEMA,
            run_id=self.run_id,
            operation_id=_uuid4(operation_id),
            effect_ordinal=self.effect_ordinal,
            request_digest=_digest(request_digest),
            plan_digest=_digest(plan_digest),
            policy_bundle_digest=_digest(policy_bundle_digest),
            preimage_terminal_event_id=preimage_terminal_event_id,
            preimage_terminal_payload_digest=_digest(
                preimage_terminal_payload_digest
            ),
            binding_digest=binding,
            publication=publication,
            state_digest="0" * 64,
        )
        raw = _record_bytes(candidate)
        target = ConsolidationPolicyPublicationRecord(
            **{
                **{
                    field: getattr(candidate, field)
                    for field in candidate.__dataclass_fields__
                    if field != "state_digest"
                },
                "state_digest": hashlib.sha256(raw).hexdigest(),
            }
        )
        with _authority(self.vault_root, mutation=True):
            existing = self._read_optional()
            if existing is None:
                reserved_paths._publish_owner_bytes(  # noqa: SLF001
                    self.vault_root,
                    self.path,
                    _DESCRIPTOR_ID,
                    raw,
                    require_missing=True,
                )
                return target
            current = _parse_record(existing)
            if (
                current.run_id != self.run_id
                or current.effect_ordinal != self.effect_ordinal
                or current != target
            ):
                _fail()
            return current


_EFFECT_SCHEMA = "exomem.consolidation-policy-activation-effect/v1"
_MIRROR_SCHEMA = "exomem.consolidation-policy-workspace-mirror/v1"


def _crash_point(_point: str) -> None:
    """Narrow test seam around durable policy activation boundaries."""


def _effect_digest(kind: str, state: str, facts: Mapping[str, object]) -> str:
    try:
        raw = consolidation_plan.canonical_closed_jcs(
            {
                "schema": _EFFECT_SCHEMA,
                "kind": kind,
                "state": state,
                "facts": dict(facts),
            }
        )
    except consolidation_plan.ConsolidationPlanUnavailable:
        _fail()
    domain = _EFFECT_SCHEMA.encode("ascii")
    return hashlib.sha256(
        len(domain).to_bytes(4, "big")
        + domain
        + len(raw).to_bytes(8, "big")
        + raw
    ).hexdigest()


def _observation(
    state: str,
    digest: str,
) -> consolidation_effect_coordinator.EffectObservation:
    return consolidation_effect_coordinator.EffectObservation(  # type: ignore[arg-type]
        state=state,
        digest=digest,
    )


def _policy_receipt(
    record: ConsolidationPolicyPublicationRecord,
) -> policy_publication.CriticalReceipt:
    publication = record.publication
    grants = publication.dependent_grants or ()
    prior = _effect_digest(
        "governance-policy-publication",
        "prior",
        {
            "activation_state_digest": publication.expected.activation_state_digest,
            "policy_generation_id": publication.expected.policy_generation_id,
            "dependent_grant_ids": [item.grant_id for item in grants],
        },
    )
    prepared = _effect_digest(
        "governance-policy-publication",
        "prepared",
        {
            "policy_generation_id": publication.policy.generation_id,
            "policy_fingerprint": publication.policy.policy_fingerprint,
            "namespace_id": publication.namespace.namespace_id,
            "dependent_grant_ids": [item.grant_id for item in grants],
        },
    )
    target = _effect_digest(
        "governance-policy-publication",
        "target",
        {"binding_digest": record.binding_digest},
    )
    affected = tuple(
        sorted(
            {
                hashlib.sha256(
                    f"policy_generation:{publication.policy.generation_id}".encode()
                ).hexdigest(),
                *(
                    hashlib.sha256(
                        f"dependent_grant:{item.grant_id}".encode()
                    ).hexdigest()
                    for item in grants
                ),
            }
        )
    )
    return policy_publication.CriticalReceipt(
        event_id=publication.identity.receipt_event_id,
        operation="governance_policy_publication",
        prior=prior,
        prepared=prepared,
        target=target,
        affected_ids=affected,
    )


def _workspace_mirror(
    record: ConsolidationPolicyPublicationRecord,
) -> policy_publication.WorkspaceMirror:
    publication = record.publication
    event_id = receipts.critical_event_id(
        {
            "schema": _MIRROR_SCHEMA,
            "policy_receipt_event_id": publication.identity.receipt_event_id,
            "policy_generation_id": publication.identity.policy_generation_id,
            "binding_digest": record.binding_digest,
        }
    )
    source_documents = [
        {
            "path_digest": hashlib.sha256(relative.encode("utf-8")).hexdigest(),
            "sha256": hashlib.sha256(content).hexdigest(),
        }
        for relative, content in publication.policy.source_documents
    ]
    receipt = policy_publication.CriticalReceipt(
        event_id=event_id,
        operation="governance_policy_workspace_mirror",
        prior=_effect_digest(
            "governance-policy-workspace-mirror",
            "prior",
            {
                "policy_generation_id": publication.expected.policy_generation_id,
                "authoring_snapshot_digest": record.binding_digest,
            },
        ),
        prepared=_effect_digest(
            "governance-policy-workspace-mirror",
            "prepared",
            {
                "policy_generation_id": publication.policy.generation_id,
                "policy_receipt_event_id": publication.identity.receipt_event_id,
                "source_fingerprint": publication.policy.source_fingerprint,
            },
        ),
        target=_effect_digest(
            "governance-policy-workspace-mirror",
            "target",
            {
                "policy_generation_id": publication.policy.generation_id,
                "source_documents": source_documents,
            },
        ),
        affected_ids=(
            hashlib.sha256(
                consolidation_plan.canonical_closed_jcs(
                    sorted(relative for relative, _content in publication.policy.source_documents)
                )
            ).hexdigest(),
        ),
        parent_causation_id=publication.identity.receipt_event_id,
    )
    return policy_publication.WorkspaceMirror(
        receipt=receipt,
        outcomes=frozenset({"complete", "diverged"}),
    )


def _preimage_parent(
    vault_root: Path,
    *,
    result: consolidation_apply_coordinator.ApplyPreparationResult,
    run_id: str,
    operation_id: str,
    request_digest: str,
) -> int:
    terminal = result.preimage_effect.terminal
    if terminal.event_id != result.preimage_effect.intent.event_id + ":committed":
        _fail()
    matches = [
        row
        for row in consolidation_receipts._active_records(vault_root)  # noqa: SLF001
        if row.get("event_type") == "consolidation"
        and row.get("phase") == "committed"
        and row.get("event_id") == terminal.event_id
    ]
    if len(matches) != 1:
        _fail()
    try:
        nested = consolidation_receipts.validate_nested(
            matches[0].get("consolidation_event"),
            outer_phase="committed",
        )
    except consolidation_receipts.ConsolidationReceiptUnavailable:
        _fail()
    if (
        nested["kind"] != "preimage"
        or nested["run_id"] != run_id
        or nested["operation_id"] != operation_id
        or nested["request_digest"] != request_digest
        or nested["payload_digest"] != terminal.payload_digest
    ):
        _fail()
    return int(nested["effect_ordinal"])


def _timestamp_seconds(value: str) -> int:
    try:
        _text, parsed = consolidation_plan._timestamp(value)  # noqa: SLF001
    except consolidation_plan.ConsolidationPlanUnavailable:
        _fail()
    timestamp = int(parsed.timestamp())
    if timestamp < 0:
        _fail()
    return timestamp


def _policy_verification_timestamp() -> str:
    """Return server-current time for live destination-attestation checks."""

    return datetime.now(UTC).isoformat(timespec="milliseconds").replace(
        "+00:00",
        "Z",
    )


def _authorization_now() -> int:
    """Return server-current epoch time for custody and tuple publication."""

    return int(datetime.now(UTC).timestamp())


def _current_authorization_now() -> int:
    """Sample and validate authorization time at the custody operation."""

    current = _authorization_now()
    if type(current) is not int or current < 0:
        _fail()
    return current


def activate_stored_destination_policy(
    *,
    vault_root: Path | str,
    admission: consolidation_admission.ConsolidationAdmission,
    preparation: consolidation_apply_coordinator.ApplyPreparationResult,
    vault_binding_digest: str,
    run_id: str,
    operation_id: str,
    journal_digest: str,
    request_digest: str,
    plan_digest: str,
    principal_contexts: Sequence[RequestPrincipal],
    policy_prepare_at: str,
    policy_active_at: str,
    timeout: float,
) -> ConsolidationPolicyActivationResult:
    """Activate only the stored destination policy after the verified preimage."""

    root = Path(vault_root).absolute()
    checked_run = _uuid4(run_id)
    checked_operation = _uuid4(operation_id)
    checked_vault = _digest(vault_binding_digest)
    checked_journal = _digest(journal_digest)
    checked_request = _digest(request_digest)
    checked_plan = _digest(plan_digest)
    if (
        not isinstance(admission, consolidation_admission.ConsolidationAdmission)
        or admission.vault_root != root
        or not isinstance(
            preparation,
            consolidation_apply_coordinator.ApplyPreparationResult,
        )
        or not isinstance(timeout, (int, float))
        or isinstance(timeout, bool)
        or timeout < 0
    ):
        _fail()
    try:
        _timestamp_seconds(policy_prepare_at)
        _timestamp_seconds(policy_active_at)
        parent_ordinal = _preimage_parent(
            root,
            result=preparation,
            run_id=checked_run,
            operation_id=checked_operation,
            request_digest=checked_request,
        )
        plan_store = consolidation_plan_store.ConsolidationPlanStore(root)
        stored_plan = plan_store.load(
            checked_run,
            plan_kind="cutover",
            plan_digest=checked_plan,
        )
        bundle = plan_store.load_policy_bundle(
            checked_run,
            plan_kind="cutover",
            plan_digest=checked_plan,
        )
        if (
            stored_plan.preimage["policy_bundle_digest"] != bundle.digest
            or stored_plan.preimage["nonce"] != bundle.nonce
            or stored_plan.preimage["run_id"] != checked_run
        ):
            _fail()
        prepare_store = ConsolidationPolicyPublicationStore(
            root,
            run_id=checked_run,
            effect_ordinal=parent_ordinal + 1,
        )
        prepare_facts = {
            "plan_digest": checked_plan,
            "policy_bundle_digest": bundle.digest,
            "preimage_terminal_event_id": preparation.preimage_effect.terminal.event_id,
            "preimage_terminal_payload_digest": preparation.preimage_effect.terminal.payload_digest,
        }
        prepare_prior = _effect_digest("policy-prepare", "prior", prepare_facts)
        prepare_target = _effect_digest("policy-prepare", "target", prepare_facts)
        prepare_evidence = _effect_digest("policy-prepare", "prepared", prepare_facts)

        def stored_record() -> ConsolidationPolicyPublicationRecord | None:
            record = prepare_store.load_optional()
            if record is None:
                return None
            if (
                record.operation_id != checked_operation
                or record.request_digest != checked_request
                or record.plan_digest != checked_plan
                or record.policy_bundle_digest != bundle.digest
                or record.preimage_terminal_event_id
                != preparation.preimage_effect.terminal.event_id
                or record.preimage_terminal_payload_digest
                != preparation.preimage_effect.terminal.payload_digest
            ):
                _fail()
            return record

        def classify_prepare() -> consolidation_effect_coordinator.EffectObservation:
            record = stored_record()
            if record is None:
                return _observation("prior", prepare_prior)
            terminal = policy_publication.receipt_terminal(root, _policy_receipt(record))
            if terminal is None:
                return _observation("prepared", prepare_evidence)
            if terminal in {"pending", "committed"}:
                return _observation("target", prepare_target)
            _fail()

        def fresh_revalidate() -> None:
            consolidation_policy.revalidate_destination_policy(
                root,
                bundle,
                principal_contexts=principal_contexts,
                destination_vault_id=bundle.destination_vault_id,
                expected_nonce=bundle.nonce,
                verified_at=_policy_verification_timestamp(),
            )

        def prepare_policy() -> None:
            fresh_revalidate()
            identity = derive_policy_publication_identity(
                destination_vault_id=bundle.destination_vault_id,
                vault_binding_digest=checked_vault,
                run_id=checked_run,
                operation_id=checked_operation,
                request_digest=checked_request,
                plan_digest=checked_plan,
                policy_bundle_digest=bundle.digest,
                preimage_terminal_event_id=preparation.preimage_effect.terminal.event_id,
                preimage_terminal_payload_digest=(
                    preparation.preimage_effect.terminal.payload_digest
                ),
                policy_prepared_at=policy_prepare_at,
            )
            publication = policy_publication.prepare_destination_policy_publication(
                root,
                prospective=bundle.prospective,
                document_edits=dict(bundle.document_edits),
                generation_id=identity.generation_id,
                authoring_event_id=identity.authoring_event_id,
                receipt_event_id=identity.receipt_event_id,
                ready_at=_timestamp_seconds(policy_prepare_at),
                now=_current_authorization_now(),
            )
            record = prepare_store.create(
                operation_id=checked_operation,
                request_digest=checked_request,
                plan_digest=checked_plan,
                policy_bundle_digest=bundle.digest,
                preimage_terminal_event_id=preparation.preimage_effect.terminal.event_id,
                preimage_terminal_payload_digest=(
                    preparation.preimage_effect.terminal.payload_digest
                ),
                publication=publication,
            )
            _crash_point("after-policy-record")
            policy_publication.begin_receipt(root, _policy_receipt(record))
            _crash_point("after-inner-receipt-intent")

        def resume_prepare() -> None:
            fresh_revalidate()
            record = stored_record()
            if record is None:
                _fail()
            policy_publication.begin_receipt(root, _policy_receipt(record))
            _crash_point("after-inner-receipt-intent")

        prepare_event = consolidation_receipts.build_intent(
            kind="policy-prepare",
            run_id=checked_run,
            operation_id=checked_operation,
            phase="policy-prepare",
            effect_ordinal=parent_ordinal + 1,
            request_digest=checked_request,
            prior_digest=prepare_prior,
            prepared_digest=prepare_evidence,
            target_digest=prepare_target,
            evidence=consolidation_receipts.build_evidence(
                kind="policy-prepare",
                digests={
                    "policy_bundle_digest": bundle.digest,
                    "policy_prepared_digest": prepare_evidence,
                },
            ),
            semantic_parent_event_id=preparation.preimage_effect.terminal.event_id,
            semantic_parent_payload_digest=(
                preparation.preimage_effect.terminal.payload_digest
            ),
        )
        policy_prepare = consolidation_effect_coordinator._execute_effect(  # noqa: SLF001
            vault_root=root,
            event=prepare_event,
            journal=consolidation_effect_coordinator.ConsolidationEffectJournalStore(
                root,
                run_id=checked_run,
                effect_ordinal=parent_ordinal + 1,
            ),
            classify=classify_prepare,
            apply_effect=prepare_policy,
            resume_effect=resume_prepare,
            timestamp=policy_prepare_at,
        )
        record = stored_record()
        if record is None:
            _fail()
        active_facts = {
            "policy_bundle_digest": bundle.digest,
            "policy_prepared_digest": record.binding_digest,
            "policy_prepare_terminal": policy_prepare.terminal.event_id,
        }
        active_prior = _effect_digest("policy-active", "prior", active_facts)
        active_prepared = _effect_digest("policy-active", "prepared", active_facts)
        active_target = _effect_digest("policy-active", "target", active_facts)

        def seal_is_target() -> bool:
            state = admission.reload().state
            return (
                state.kind == "consolidation-sealed"
                and state.phase == "policy-active"
                and state.vault_binding_digest == checked_vault
                and state.run_id == checked_run
                and state.operation_id == checked_operation
                and state.journal_digest == checked_journal
            )

        def seal_is_preimage_ready() -> bool:
            state = admission.reload().state
            return (
                state.kind == "consolidation-sealed"
                and state.phase == "preimage-ready"
                and state.vault_binding_digest == checked_vault
                and state.run_id == checked_run
                and state.operation_id == checked_operation
                and state.journal_digest == checked_journal
            )

        def classify_active() -> consolidation_effect_coordinator.EffectObservation:
            authority = policy_publication.classify_authority(
                root,
                record.publication,
                now=_current_authorization_now(),
            )
            if authority.state == "mixed":
                return _observation("mixed", record.binding_digest)
            if authority.state == "prior":
                return _observation("prior", active_prior)
            if authority.state == "tuple-committed":
                return _observation("prepared", active_prepared)
            if authority.state != "active":
                _fail()
            if policy_publication.receipt_terminal(root, _policy_receipt(record)) != "committed":
                return _observation("prepared", active_prepared)
            mirror_terminal = policy_publication.workspace_mirror_terminal(
                root,
                _workspace_mirror(record),
            )
            if mirror_terminal == "diverged":
                return _observation("mixed", record.binding_digest)
            if mirror_terminal == "complete":
                prepared_mirror = policy_publication.prepare_workspace_mirror(
                    record.publication,
                    mirror=_workspace_mirror(record),
                    reviewed=bundle.prospective.snapshot,
                )
                if not policy_publication.prepared_workspace_mirror_matches(
                    root,
                    prepared_mirror,
                ):
                    return _observation("mixed", record.binding_digest)
            if mirror_terminal == "complete" and seal_is_target():
                return _observation("target", active_target)
            if mirror_terminal in {None, "complete"}:
                return _observation("prepared", active_prepared)
            _fail()

        def finish_active() -> None:
            if not (seal_is_preimage_ready() or seal_is_target()):
                _fail()
            before = policy_publication.classify_authority(
                root,
                record.publication,
                now=_current_authorization_now(),
            )
            if before.state == "mixed":
                _fail()
            if before.state == "prior":
                # Until CAS commits the exact successor, authorization remains
                # mutable authority and its attestations must still be fresh.
                fresh_revalidate()
            classification = policy_publication.activate_or_recover(
                root,
                record.publication,
                now=_current_authorization_now(),
            )
            if classification.state == "stale" or classification.active is None:
                _fail()
            _crash_point("after-tuple-custody-activation")
            receipt = _policy_receipt(record)
            policy_publication.commit_receipt(root, receipt)
            _crash_point("after-inner-terminal")
            mirror = policy_publication.prepare_workspace_mirror(
                record.publication,
                mirror=_workspace_mirror(record),
                reviewed=bundle.prospective.snapshot,
            )
            with reserved_paths._owner_authority_scope(  # noqa: SLF001
                "govern_memory"
            ):
                outcome = policy_publication.run_prepared_workspace_mirror(
                    root,
                    mirror,
                )
            if outcome != "complete":
                _fail()
            if not policy_publication.prepared_workspace_mirror_matches(root, mirror):
                _fail()
            _crash_point("after-mirror")
            state = admission.reload().state
            if state.phase == "preimage-ready":
                authority = consolidation_authority.issue_authority(
                    vault_binding_digest=checked_vault,
                    run_id=checked_run,
                    operation_id=checked_operation,
                    journal_digest=checked_journal,
                    phase="preimage-ready",
                    action="apply",
                )
                consolidation_seal.ConsolidationSealStore(root).advance_consolidation(
                    authority,
                    vault_binding_digest=checked_vault,
                    action="apply",
                    target_phase="policy-active",
                    recorded_at=policy_active_at,
                    expected_revision=state.revision,
                )
            elif not seal_is_target():
                _fail()
            _crash_point("after-seal-advance")

        active_event = consolidation_receipts.build_intent(
            kind="policy-active",
            run_id=checked_run,
            operation_id=checked_operation,
            phase="policy-active",
            effect_ordinal=parent_ordinal + 2,
            request_digest=checked_request,
            prior_digest=active_prior,
            prepared_digest=active_prepared,
            target_digest=active_target,
            evidence=consolidation_receipts.build_evidence(
                kind="policy-active",
                digests={
                    "policy_bundle_digest": bundle.digest,
                    "policy_active_digest": active_target,
                    "policy_fingerprint": (
                        record.publication.policy.policy_fingerprint
                    ),
                },
            ),
            semantic_parent_event_id=policy_prepare.terminal.event_id,
            semantic_parent_payload_digest=policy_prepare.terminal.payload_digest,
        )
        policy_active = consolidation_effect_coordinator._execute_effect(  # noqa: SLF001
            vault_root=root,
            event=active_event,
            journal=consolidation_effect_coordinator.ConsolidationEffectJournalStore(
                root,
                run_id=checked_run,
                effect_ordinal=parent_ordinal + 2,
            ),
            classify=classify_active,
            apply_effect=finish_active,
            resume_effect=finish_active,
            timestamp=policy_active_at,
        )
        state = admission.reload().state
        if not seal_is_target():
            _fail()
        terminal = consolidation_saga.PolicyActivationTerminal(
            schema=consolidation_saga.POLICY_ACTIVATION_TERMINAL_SCHEMA,
            policy_fingerprint=record.publication.policy.policy_fingerprint,
            intent_event_id=policy_active.intent.event_id,
            prepared_fingerprint=policy_prepare.observed_digest,
            active_fingerprint=policy_active.observed_digest,
            terminal_event_id=policy_active.terminal.event_id,
        )
        return ConsolidationPolicyActivationResult(
            policy_prepare=policy_prepare,
            policy_active=policy_active,
            terminal=terminal,
            seal_state=state,
        )
    except ConsolidationPolicyActivationUnavailable:
        raise
    except (
        consolidation_admission.ConsolidationAdmissionUnavailable,
        consolidation_authority.ConsolidationAuthorityUnavailable,
        consolidation_effect_coordinator.ConsolidationEffectUnavailable,
        consolidation_plan_store.ConsolidationPlanStoreUnavailable,
        consolidation_receipts.ConsolidationReceiptUnavailable,
        consolidation_seal.ConsolidationSealUnavailable,
        policy_publication.GovernanceError,
        OSError,
        RuntimeError,
        TypeError,
        ValueError,
    ):
        _fail()
