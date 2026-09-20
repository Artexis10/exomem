"""Private writer-owned recovery evidence for hosted canonical mutations.

Prepared payload authentication is not commit proof.  This module mints commit
evidence only from immutable journal components inserted beside an actual tuple
publication in the caller's SQLite transaction.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sqlite3
import stat
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import hosted_mutation_recovery
from .transaction import canonical_json, digest

OPERATION = "hosted_mutation_commit_v1"
OWNER = "writer_canonical_v1"
PLAN_KIND = "hosted-mutation-plan/v1"
CHILD_KIND = "hosted-mutation-child/v1"
COMMIT_KIND = "hosted-mutation-commit/v1"
TERMINAL_KIND = "hosted-mutation-terminal/v1"

_PLAN_SCHEMA = "exomem.hosted-mutation-plan/v1"
_CHILD_SCHEMA = "exomem.hosted-mutation-child/v1"
_COMMIT_SCHEMA = "exomem.hosted-mutation-commit/v1"
_TERMINAL_SCHEMA = "exomem.hosted-mutation-terminal/v1"
_SIDECAR_RECEIPT_SCHEMA = "exomem.hosted-mutation-sidecar-receipt/v1"
_PLAN_DOMAIN = b"exomem.hosted-mutation-plan-binding/v1\x00"
_CHILD_DOMAIN = b"exomem.hosted-mutation-child-binding/v1\x00"
_COMMIT_DOMAIN = b"exomem.hosted-mutation-commit-binding/v1\x00"
_TERMINAL_DOMAIN = b"exomem.hosted-mutation-terminal-binding/v1\x00"
_SIDECAR_RECEIPT_DOMAIN = b"exomem.hosted-mutation-sidecar-receipt/v1\x00"
_DEPENDENCY_KEYS = (
    "catalog_generations",
    "component_keys",
    "measurement_stores",
    "policy_generations",
    "projection_namespaces",
    "publications",
)


class HostedMutationJournalError(RuntimeError):
    """Writer recovery evidence is absent, malformed, or inconsistent."""


@dataclass(frozen=True, slots=True)
class PreparedCanonicalMutationRecovery:
    """Attempt-authenticated private preparation, without any commit claim."""

    descriptor: Mapping[str, object]
    descriptor_sha256: str
    descriptor_mac: str
    prepared_results: Mapping[str, Mapping[str, object]]
    result_macs: Mapping[str, str]

    def to_json(self) -> str:
        return canonical_json(
            {
                "descriptor": self.descriptor,
                "descriptor_sha256": self.descriptor_sha256,
                "descriptor_mac": self.descriptor_mac,
                "prepared_results": self.prepared_results,
                "result_macs": self.result_macs,
            }
        )


def prepared_recovery_from_json(value: str) -> PreparedCanonicalMutationRecovery:
    if not isinstance(value, str) or not 1 <= len(value.encode("utf-8")) <= (
        hosted_mutation_recovery.MAX_CANONICAL_BYTES
    ):
        raise HostedMutationJournalError("prepared recovery payload exceeds its bound")
    try:
        payload = json.loads(value)
    except (TypeError, ValueError):
        raise HostedMutationJournalError("prepared recovery payload is malformed") from None
    fields = {
        "descriptor",
        "descriptor_sha256",
        "descriptor_mac",
        "prepared_results",
        "result_macs",
    }
    if not isinstance(payload, dict) or set(payload) != fields:
        raise HostedMutationJournalError("prepared recovery payload is malformed")
    if not isinstance(payload["prepared_results"], dict) or not isinstance(
        payload["result_macs"], dict
    ):
        raise HostedMutationJournalError("prepared recovery payload is malformed")
    return PreparedCanonicalMutationRecovery(
        descriptor=payload["descriptor"],
        descriptor_sha256=payload["descriptor_sha256"],
        descriptor_mac=payload["descriptor_mac"],
        prepared_results=payload["prepared_results"],
        result_macs=payload["result_macs"],
    )


@dataclass(frozen=True, slots=True)
class HostedMutationPlan:
    event_id: str
    descriptor_sha256: str


@dataclass(frozen=True, slots=True)
class CompleteCanonicalEvidence:
    """A complete immutable child set, still awaiting terminal persistence."""

    event_id: str
    component_sha256: str | None
    complete: bool


@dataclass(frozen=True, slots=True)
class PreparedHostedMutationChild:
    recovery: PreparedCanonicalMutationRecovery
    child_id: str
    attempt_secret: bytes = field(repr=False)
    now: float


@dataclass(frozen=True, slots=True)
class PublicationSelectorEvidence:
    descriptor_sha256: str
    child_id: str | None


@dataclass(frozen=True, slots=True)
class PublicationTupleEvidence:
    event_id: str
    publication_kind: str
    predecessor_activation_state_digest: str
    target_activation_state_digest: str
    policy_generation_id: str
    policy_fingerprint: str
    projector_schema_version: int
    catalog_generation: int
    activation_epoch: int
    activated_at: int


@dataclass(frozen=True, slots=True)
class CommittedPublicationEvidence:
    component_kind: str
    component_sha256: str
    descriptor_sha256: str
    child_id: str | None
    cell_id: str
    logical_vault_id: str
    registry_attachment_id: str
    attachment_epoch: int
    activation_store_id: str
    publication: PublicationTupleEvidence


_VERIFIED_COMPLETE_TOKEN = object()


@dataclass(frozen=True, slots=True)
class VerifiedCompleteCanonicalEvidence:
    """Complete evidence minted by this module, required by the renderer."""

    evidence: CommittedPublicationEvidence
    _token: object = field(repr=False)

    def __post_init__(self) -> None:
        if self._token is not _VERIFIED_COMPLETE_TOKEN:
            raise HostedMutationJournalError("complete canonical evidence is invalid")


def _sha(value: str | bytes) -> str:
    encoded = value.encode("utf-8") if isinstance(value, str) else value
    return hashlib.sha256(encoded).hexdigest()


def _component_hash(encoded: str) -> str:
    try:
        value = json.loads(encoded)
    except (TypeError, ValueError):
        raise HostedMutationJournalError("journal component is malformed") from None
    if canonical_json(value) != encoded:
        raise HostedMutationJournalError("journal component is not canonical")
    return digest(value)


def _mac(domain: bytes, payload: Mapping[str, object], secret: bytes) -> str:
    if not isinstance(secret, bytes) or len(secret) != 32:
        raise HostedMutationJournalError("attempt secret is invalid")
    return hmac.new(secret, domain + canonical_json(payload).encode("utf-8"), hashlib.sha256).hexdigest()


def _bound_payload(
    domain: bytes, payload: Mapping[str, object], secret: bytes
) -> dict[str, object]:
    return {**payload, "mac": _mac(domain, payload, secret)}


def _verify_bound_payload(
    domain: bytes,
    payload: object,
    *,
    fields: frozenset[str],
    secret: bytes,
) -> dict[str, object]:
    if not isinstance(payload, dict) or set(payload) != fields | {"mac"}:
        raise HostedMutationJournalError("journal component is malformed")
    supplied = payload.get("mac")
    unsigned = {key: value for key, value in payload.items() if key != "mac"}
    expected = _mac(domain, unsigned, secret)
    valid = isinstance(supplied, str) and len(supplied) == 64
    candidate = supplied if valid else "0" * 64
    if not hmac.compare_digest(expected, candidate) or not valid:
        raise HostedMutationJournalError("journal component binding is invalid")
    return unsigned


def prepare_canonical_mutation_recovery(
    *,
    descriptor: Mapping[str, object],
    prepared_results: Mapping[str, Mapping[str, object]],
    attempt_secret: bytes,
) -> PreparedCanonicalMutationRecovery:
    """Validate and authenticate all private preparation before effects."""

    validated_descriptor = hosted_mutation_recovery.validate_descriptor(descriptor)
    results = hosted_mutation_recovery.validate_prepared_payloads(
        validated_descriptor, prepared_results
    )
    descriptor_sha256 = hosted_mutation_recovery.descriptor_digest(validated_descriptor)
    descriptor_mac = hosted_mutation_recovery.bind_descriptor(
        validated_descriptor, attempt_secret=attempt_secret
    )
    result_macs = {
        child["id"]: hosted_mutation_recovery.bind_child_prepared_result(
            validated_descriptor,
            child_id=child["id"],
            result=results[child["id"]],
            attempt_secret=attempt_secret,
        )
        for child in validated_descriptor["required_children"]
    }
    return PreparedCanonicalMutationRecovery(
        descriptor=validated_descriptor,
        descriptor_sha256=descriptor_sha256,
        descriptor_mac=descriptor_mac,
        prepared_results=results,
        result_macs=result_macs,
    )


def validate_prepared_recovery(
    recovery: PreparedCanonicalMutationRecovery, *, attempt_secret: bytes
) -> PreparedCanonicalMutationRecovery:
    if not isinstance(recovery, PreparedCanonicalMutationRecovery):
        raise HostedMutationJournalError("prepared recovery is invalid")
    expected = prepare_canonical_mutation_recovery(
        descriptor=recovery.descriptor,
        prepared_results=recovery.prepared_results,
        attempt_secret=attempt_secret,
    )
    if not (
        hmac.compare_digest(expected.descriptor_sha256, recovery.descriptor_sha256)
        and hmac.compare_digest(expected.descriptor_mac, recovery.descriptor_mac)
        and set(expected.result_macs) == set(recovery.result_macs)
        and all(
            isinstance(recovery.result_macs.get(child_id), str)
            and hmac.compare_digest(mac, recovery.result_macs[child_id])
            for child_id, mac in expected.result_macs.items()
        )
    ):
        raise HostedMutationJournalError("prepared recovery binding is invalid")
    return expected


def _dependencies(value: Mapping[str, object]) -> dict[str, list[object]]:
    if not isinstance(value, Mapping) or any(key not in _DEPENDENCY_KEYS for key in value):
        raise HostedMutationJournalError("dependency manifest is invalid")
    result: dict[str, list[object]] = {}
    for key in _DEPENDENCY_KEYS:
        raw = value.get(key, [])
        if not isinstance(raw, (list, tuple)):
            raise HostedMutationJournalError("dependency manifest is invalid")
        items: list[object] = []
        for item in raw:
            if key == "catalog_generations":
                if isinstance(item, bool) or not isinstance(item, int) or item < 0:
                    raise HostedMutationJournalError("dependency manifest is invalid")
            elif not isinstance(item, str) or not item or len(item.encode("utf-8")) > 512:
                raise HostedMutationJournalError("dependency manifest is invalid")
            items.append(item)
        if len(set(items)) != len(items):
            raise HostedMutationJournalError("dependency manifest is invalid")
        result[key] = items
    return result


def _event_id(descriptor_sha256: str) -> str:
    return f"hosted-mutation:{descriptor_sha256}"


def _plan_payload(
    recovery: PreparedCanonicalMutationRecovery,
    *,
    dependencies: Mapping[str, object],
    attempt_secret: bytes,
) -> dict[str, object]:
    descriptor = recovery.descriptor
    normalized_dependencies = _dependencies(dependencies)
    required = descriptor["required_children"]
    unsigned = {
        "schema": _PLAN_SCHEMA,
        "owner": OWNER,
        "scoped_idempotency_digest": descriptor["scoped_idempotency_digest"],
        "command_digest": descriptor["command_digest"],
        "attempt_id": descriptor["attempt_id"],
        "commit_token": descriptor["commit_token"],
        "descriptor_sha256": recovery.descriptor_sha256,
        "private_payload_sha256": _sha(recovery.to_json()),
        "required_children_sha256": _sha(canonical_json(required)),
        "dependency_manifest_sha256": _sha(canonical_json(normalized_dependencies)),
        "required_children": required,
        "dependencies": normalized_dependencies,
        "identity": {
            "cell_id": descriptor["cell_id"],
            "logical_vault_id": descriptor["logical_vault_id"],
            "registry_attachment_id": descriptor["registry_attachment_id"],
            "attachment_epoch": descriptor["attachment_epoch"],
            "activation_store_id": descriptor["activation_store_id"],
        },
    }
    return _bound_payload(_PLAN_DOMAIN, unsigned, attempt_secret)


def _insert_component(
    connection: sqlite3.Connection,
    *,
    event_id: str,
    phase: str,
    ordinal: int,
    kind: str,
    key: str,
    payload: Mapping[str, object],
) -> str:
    encoded = canonical_json(payload)
    component_digest = digest(payload)
    connection.execute(
        "INSERT INTO governance_operation_components "
        "(event_id, phase, ordinal, component_kind, component_key, value_json, value_hash, status) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 'complete')",
        (event_id, phase, ordinal, kind, key, encoded, component_digest),
    )
    return component_digest


def create_allocating_journal(
    connection: sqlite3.Connection,
    *,
    recovery: PreparedCanonicalMutationRecovery,
    attempt_secret: bytes,
    dependency_manifest: Mapping[str, object],
    now: float,
) -> HostedMutationPlan:
    """Persist the complete plan before the first canonical child effect."""

    prepared = validate_prepared_recovery(recovery, attempt_secret=attempt_secret)
    plan = _plan_payload(
        prepared, dependencies=dependency_manifest, attempt_secret=attempt_secret
    )
    event_id = _event_id(prepared.descriptor_sha256)
    owns_transaction = not connection.in_transaction
    if owns_transaction:
        connection.execute("BEGIN IMMEDIATE")
    try:
        existing = connection.execute(
            "SELECT operation, principal_id, phase, prepared_digest FROM governance_operation_journals "
            "WHERE event_id=?",
            (event_id,),
        ).fetchone()
        if existing is None:
            required_ids = [child["id"] for child in prepared.descriptor["required_children"]]
            connection.execute(
                "INSERT INTO governance_operation_journals "
                "(event_id, operation, causation_id, authorization_session, principal_id, phase, "
                "direction, prior_digest, prepared_digest, final_digest, affected_ids, "
                "required_child_intents, required_child_terminals, proposal_id, attempt_no, "
                "marker_required, created_at, updated_at, blocked_reason) "
                "VALUES (?, ?, ?, NULL, ?, 'allocating', 'forward', ?, ?, ?, ?, ?, '[]', NULL, NULL, 0, ?, ?, NULL)",
                (
                    event_id,
                    OPERATION,
                    prepared.descriptor["attempt_id"],
                    OWNER,
                    "0" * 64,
                    prepared.descriptor_sha256,
                    "0" * 64,
                    canonical_json(required_ids),
                    canonical_json(required_ids),
                    now,
                    now,
                ),
            )
            _insert_component(
                connection,
                event_id=event_id,
                phase="prepared",
                ordinal=0,
                kind=PLAN_KIND,
                key=prepared.descriptor_sha256,
                payload=plan,
            )
        else:
            if existing[:2] != (OPERATION, OWNER) or existing[3] != prepared.descriptor_sha256:
                raise HostedMutationJournalError("hosted mutation journal identity changed")
            _load_plan(connection, event_id=event_id, attempt_secret=attempt_secret)
        if owns_transaction:
            connection.commit()
    except BaseException:
        if owns_transaction:
            connection.rollback()
        raise
    return HostedMutationPlan(event_id, prepared.descriptor_sha256)


_PLAN_FIELDS = frozenset(
    {
        "schema",
        "owner",
        "scoped_idempotency_digest",
        "command_digest",
        "attempt_id",
        "commit_token",
        "descriptor_sha256",
        "private_payload_sha256",
        "required_children_sha256",
        "dependency_manifest_sha256",
        "required_children",
        "dependencies",
        "identity",
    }
)


def _load_plan(
    connection: sqlite3.Connection, *, event_id: str, attempt_secret: bytes
) -> dict[str, object]:
    row = connection.execute(
        "SELECT value_json, value_hash FROM governance_operation_components "
        "WHERE event_id=? AND phase='prepared' AND ordinal=0 "
        "AND component_kind=?",
        (event_id, PLAN_KIND),
    ).fetchone()
    if row is None or _component_hash(str(row[0])) != row[1]:
        raise HostedMutationJournalError("hosted mutation plan is unavailable")
    try:
        payload = json.loads(str(row[0]))
    except (TypeError, ValueError):
        raise HostedMutationJournalError("hosted mutation plan is malformed") from None
    unsigned = _verify_bound_payload(
        _PLAN_DOMAIN, payload, fields=_PLAN_FIELDS, secret=attempt_secret
    )
    if unsigned.get("schema") != _PLAN_SCHEMA or unsigned.get("owner") != OWNER:
        raise HostedMutationJournalError("hosted mutation plan is malformed")
    if _sha(canonical_json(unsigned.get("required_children"))) != unsigned.get(
        "required_children_sha256"
    ):
        raise HostedMutationJournalError("hosted mutation child manifest changed")
    dependencies = _dependencies(unsigned.get("dependencies", {}))
    if _sha(canonical_json(dependencies)) != unsigned.get("dependency_manifest_sha256"):
        raise HostedMutationJournalError("hosted mutation dependencies changed")
    return unsigned


_PUBLICATION_FIELDS = (
    "event_id",
    "publication_kind",
    "predecessor_activation_state_digest",
    "target_activation_state_digest",
    "policy_generation_id",
    "policy_fingerprint",
    "projector_schema_version",
    "catalog_generation",
    "activation_epoch",
    "status",
    "activated_at",
)


def _publication(connection: sqlite3.Connection, event_id: str) -> dict[str, object]:
    row = connection.execute(
        "SELECT " + ",".join(_PUBLICATION_FIELDS) + " FROM governance_tuple_publications WHERE event_id=?",
        (event_id,),
    ).fetchone()
    if row is None or row[9] != "committed":
        raise HostedMutationJournalError("exact committed publication is unavailable")
    return dict(zip(_PUBLICATION_FIELDS, row, strict=True))


def _child_manifest(plan: Mapping[str, object], child_id: str) -> tuple[int, Mapping[str, object]]:
    children = plan.get("required_children")
    if not isinstance(children, list):
        raise HostedMutationJournalError("hosted mutation child manifest is malformed")
    for index, child in enumerate(children):
        if isinstance(child, dict) and child.get("id") == child_id:
            return index, child
    raise HostedMutationJournalError("hosted mutation child is not required")


def record_child_in_transaction(
    connection: sqlite3.Connection,
    *,
    recovery: PreparedCanonicalMutationRecovery,
    child_id: str,
    publication_event_id: str,
    attempt_secret: bytes,
    now: float,
) -> CompleteCanonicalEvidence:
    """Bind one child beside its already-inserted publication before commit."""

    if not connection.in_transaction:
        raise HostedMutationJournalError("child evidence requires the publication transaction")
    prepared = validate_prepared_recovery(recovery, attempt_secret=attempt_secret)
    event_id = _event_id(prepared.descriptor_sha256)
    plan = _load_plan(connection, event_id=event_id, attempt_secret=attempt_secret)
    if plan.get("private_payload_sha256") != _sha(prepared.to_json()):
        raise HostedMutationJournalError("private prepared payload changed")
    index, child = _child_manifest(plan, child_id)
    for required in child.get("requires", []):
        prior = connection.execute(
            "SELECT 1 FROM governance_operation_components WHERE event_id=? "
            "AND component_kind=? AND component_key=?",
            (event_id, CHILD_KIND, required),
        ).fetchone()
        if prior is None:
            raise HostedMutationJournalError("hosted mutation child prerequisite is absent")
    publication = _publication(connection, publication_event_id)
    result = prepared.prepared_results.get(child_id)
    result_mac = prepared.result_macs.get(child_id)
    if result is None or not hosted_mutation_recovery.verify_child_prepared_result_binding(
        prepared.descriptor,
        child_id=child_id,
        result=result,
        binding=result_mac,
        attempt_secret=attempt_secret,
    ):
        raise HostedMutationJournalError("prepared child result binding is invalid")
    unsigned = {
        "schema": _CHILD_SCHEMA,
        "owner": OWNER,
        "descriptor_sha256": prepared.descriptor_sha256,
        "attempt_id": prepared.descriptor["attempt_id"],
        "commit_token": prepared.descriptor["commit_token"],
        "child_id": child_id,
        "child_kind": child["kind"],
        "plan_sha256": child["plan_sha256"],
        "prepared_result_sha256": _sha(canonical_json(result)),
        "publication": publication,
        "completion_receipt_sha256": None,
    }
    payload = _bound_payload(_CHILD_DOMAIN, unsigned, attempt_secret)
    existing = connection.execute(
        "SELECT value_json, value_hash FROM governance_operation_components "
        "WHERE event_id=? AND component_kind=? AND component_key=?",
        (event_id, CHILD_KIND, child_id),
    ).fetchone()
    if existing is None:
        _insert_component(
            connection,
            event_id=event_id,
            phase="committed",
            ordinal=index + 1,
            kind=CHILD_KIND,
            key=child_id,
            payload=payload,
        )
    elif existing != (canonical_json(payload), digest(payload)):
        raise HostedMutationJournalError("hosted mutation child evidence changed")
    connection.execute(
        "UPDATE governance_operation_journals SET phase='pending', updated_at=? "
        "WHERE event_id=? AND phase IN ('allocating','pending')",
        (now, event_id),
    )
    return finalize_complete_evidence(
        connection,
        recovery=prepared,
        attempt_secret=attempt_secret,
        now=now,
        in_transaction=True,
    )


_SIDECAR_RECEIPT_FIELDS = frozenset(
    {
        "schema",
        "owner",
        "descriptor_sha256",
        "attempt_id",
        "commit_token",
        "child_id",
        "child_kind",
        "plan_sha256",
        "prepared_result_sha256",
        "transition_sha256",
    }
)


def prepare_sidecar_completion_receipt(
    *,
    recovery: PreparedCanonicalMutationRecovery,
    child_id: str,
    transition: Mapping[str, object],
    attempt_secret: bytes,
) -> dict[str, object]:
    """Authenticate an exact durable sidecar transition before its atomic write."""

    prepared = validate_prepared_recovery(recovery, attempt_secret=attempt_secret)
    _index, child = _child_manifest(
        {"required_children": prepared.descriptor["required_children"]}, child_id
    )
    if child.get("kind") != "sidecar":
        raise HostedMutationJournalError("completion receipt child is not a sidecar")
    result = prepared.prepared_results.get(child_id)
    if result is None:
        raise HostedMutationJournalError("prepared sidecar result is unavailable")
    try:
        transition_sha256 = _sha(canonical_json(transition))
    except (TypeError, ValueError):
        raise HostedMutationJournalError("sidecar transition is not canonical JSON data") from None
    unsigned = {
        "schema": _SIDECAR_RECEIPT_SCHEMA,
        "owner": OWNER,
        "descriptor_sha256": prepared.descriptor_sha256,
        "attempt_id": prepared.descriptor["attempt_id"],
        "commit_token": prepared.descriptor["commit_token"],
        "child_id": child_id,
        "child_kind": "sidecar",
        "plan_sha256": child["plan_sha256"],
        "prepared_result_sha256": _sha(canonical_json(result)),
        "transition_sha256": transition_sha256,
    }
    return _bound_payload(_SIDECAR_RECEIPT_DOMAIN, unsigned, attempt_secret)


def verify_sidecar_completion_receipt(
    *,
    recovery: PreparedCanonicalMutationRecovery,
    child_id: str,
    transition: Mapping[str, object],
    receipt: object,
    attempt_secret: bytes,
) -> dict[str, object]:
    """Verify a stored receipt against private preparation and exact transition."""

    expected = prepare_sidecar_completion_receipt(
        recovery=recovery,
        child_id=child_id,
        transition=transition,
        attempt_secret=attempt_secret,
    )
    try:
        supplied = _verify_bound_payload(
            _SIDECAR_RECEIPT_DOMAIN,
            receipt,
            fields=_SIDECAR_RECEIPT_FIELDS,
            secret=attempt_secret,
        )
    except HostedMutationJournalError:
        raise HostedMutationJournalError("sidecar completion receipt is invalid") from None
    if canonical_json(receipt) != canonical_json(expected):
        raise HostedMutationJournalError("sidecar completion receipt is invalid")
    return supplied


def record_sidecar_child(
    connection: sqlite3.Connection,
    *,
    recovery: PreparedCanonicalMutationRecovery,
    child_id: str,
    transition: Mapping[str, object],
    receipt: object,
    attempt_secret: bytes,
    now: float,
) -> CompleteCanonicalEvidence:
    """Register an already-atomic sidecar transition without publishing a tuple."""

    prepared = validate_prepared_recovery(recovery, attempt_secret=attempt_secret)
    event_id = _event_id(prepared.descriptor_sha256)
    owns_transaction = not connection.in_transaction
    if owns_transaction:
        connection.execute("BEGIN IMMEDIATE")
    try:
        plan = _load_plan(connection, event_id=event_id, attempt_secret=attempt_secret)
        if plan.get("private_payload_sha256") != _sha(prepared.to_json()):
            raise HostedMutationJournalError("private prepared payload changed")
        index, child = _child_manifest(plan, child_id)
        if child.get("kind") != "sidecar":
            raise HostedMutationJournalError("completion receipt child is not a sidecar")
        for required in child.get("requires", []):
            prior = connection.execute(
                "SELECT 1 FROM governance_operation_components WHERE event_id=? "
                "AND component_kind=? AND component_key=?",
                (event_id, CHILD_KIND, required),
            ).fetchone()
            if prior is None:
                raise HostedMutationJournalError(
                    "hosted mutation child prerequisite is absent"
                )
        verify_sidecar_completion_receipt(
            recovery=prepared,
            child_id=child_id,
            transition=transition,
            receipt=receipt,
            attempt_secret=attempt_secret,
        )
        result = prepared.prepared_results[child_id]
        unsigned = {
            "schema": _CHILD_SCHEMA,
            "owner": OWNER,
            "descriptor_sha256": prepared.descriptor_sha256,
            "attempt_id": prepared.descriptor["attempt_id"],
            "commit_token": prepared.descriptor["commit_token"],
            "child_id": child_id,
            "child_kind": "sidecar",
            "plan_sha256": child["plan_sha256"],
            "prepared_result_sha256": _sha(canonical_json(result)),
            "publication": None,
            "completion_receipt_sha256": _sha(canonical_json(receipt)),
        }
        payload = _bound_payload(_CHILD_DOMAIN, unsigned, attempt_secret)
        existing = connection.execute(
            "SELECT value_json, value_hash FROM governance_operation_components "
            "WHERE event_id=? AND component_kind=? AND component_key=?",
            (event_id, CHILD_KIND, child_id),
        ).fetchone()
        if existing is None:
            _insert_component(
                connection,
                event_id=event_id,
                phase="committed",
                ordinal=index + 1,
                kind=CHILD_KIND,
                key=child_id,
                payload=payload,
            )
        elif existing != (canonical_json(payload), digest(payload)):
            raise HostedMutationJournalError("hosted mutation child evidence changed")
        connection.execute(
            "UPDATE governance_operation_journals SET phase='pending', updated_at=? "
            "WHERE event_id=? AND phase IN ('allocating','pending')",
            (now, event_id),
        )
        complete = finalize_complete_evidence(
            connection,
            recovery=prepared,
            attempt_secret=attempt_secret,
            now=now,
            in_transaction=True,
        )
        if owns_transaction:
            connection.commit()
        return complete
    except BaseException:
        if owns_transaction:
            connection.rollback()
        raise


def finalize_complete_evidence(
    connection: sqlite3.Connection,
    *,
    recovery: PreparedCanonicalMutationRecovery,
    attempt_secret: bytes,
    now: float,
    in_transaction: bool = False,
) -> CompleteCanonicalEvidence:
    """Bind an aggregate only from the complete retained child set."""

    prepared = validate_prepared_recovery(recovery, attempt_secret=attempt_secret)
    event_id = _event_id(prepared.descriptor_sha256)
    owns_transaction = not in_transaction and not connection.in_transaction
    if owns_transaction:
        connection.execute("BEGIN IMMEDIATE")
    try:
        plan = _load_plan(connection, event_id=event_id, attempt_secret=attempt_secret)
        children = plan["required_children"]
        rows = connection.execute(
            "SELECT component_key, value_hash, value_json FROM governance_operation_components "
            "WHERE event_id=? AND component_kind=? ORDER BY ordinal",
            (event_id, CHILD_KIND),
        ).fetchall()
        required_ids = [child["id"] for child in children]
        if [row[0] for row in rows] != required_ids:
            if owns_transaction:
                connection.commit()
            return CompleteCanonicalEvidence(event_id, None, False)
        child_hashes: list[dict[str, str]] = []
        for child_id, row_digest, encoded in rows:
            if _component_hash(str(encoded)) != row_digest:
                raise HostedMutationJournalError("hosted mutation child evidence changed")
            child_hashes.append(
                {"child_id": str(child_id), "component_sha256": str(row_digest)}
            )
        publications = [
            component["publication"]
            for _child_id, _row_digest, encoded in rows
            if isinstance(
                (component := json.loads(str(encoded))).get("publication"), dict
            )
        ]
        if not publications:
            raise HostedMutationJournalError(
                "complete canonical evidence has no tuple publication"
            )
        final_publication = publications[-1]
        unsigned = {
            "schema": _COMMIT_SCHEMA,
            "owner": OWNER,
            "descriptor_sha256": prepared.descriptor_sha256,
            "attempt_id": prepared.descriptor["attempt_id"],
            "commit_token": prepared.descriptor["commit_token"],
            "children": child_hashes,
            "final_publication": final_publication,
        }
        payload = _bound_payload(_COMMIT_DOMAIN, unsigned, attempt_secret)
        encoded = canonical_json(payload)
        component_digest = digest(payload)
        ordinal = len(required_ids) + 1
        existing = connection.execute(
            "SELECT value_json, value_hash FROM governance_operation_components "
            "WHERE event_id=? AND component_kind=?",
            (event_id, COMMIT_KIND),
        ).fetchone()
        if existing is None:
            _insert_component(
                connection,
                event_id=event_id,
                phase="committed",
                ordinal=ordinal,
                kind=COMMIT_KIND,
                key=prepared.descriptor_sha256,
                payload=payload,
            )
            connection.execute(
                "UPDATE governance_operation_journals SET phase='pending', final_digest=?, updated_at=? "
                "WHERE event_id=? AND phase IN ('allocating','pending')",
                (component_digest, now, event_id),
            )
        elif existing != (encoded, component_digest):
            raise HostedMutationJournalError("hosted mutation aggregate evidence changed")
        if owns_transaction:
            connection.commit()
        return CompleteCanonicalEvidence(event_id, component_digest, True)
    except BaseException:
        if owns_transaction:
            connection.rollback()
        raise


def record_terminal(
    connection: sqlite3.Connection,
    *,
    recovery: PreparedCanonicalMutationRecovery,
    attempt_secret: bytes,
    terminal: Mapping[str, object],
    disposition: str,
    now: float,
) -> str:
    """Close a complete writer journal after the exact terminal is durable."""

    if disposition not in {"success", "committed_failure"}:
        raise HostedMutationJournalError("terminal disposition is invalid")
    prepared = validate_prepared_recovery(recovery, attempt_secret=attempt_secret)
    if not isinstance(terminal, Mapping):
        raise HostedMutationJournalError("terminal is not canonical JSON data")
    try:
        terminal_sha256 = _sha(canonical_json(terminal))
    except (TypeError, ValueError):
        raise HostedMutationJournalError("terminal is not canonical JSON data") from None
    event_id = _event_id(prepared.descriptor_sha256)
    owns_transaction = not connection.in_transaction
    if owns_transaction:
        connection.execute("BEGIN IMMEDIATE")
    try:
        aggregate = connection.execute(
            "SELECT value_hash FROM governance_operation_components "
            "WHERE event_id=? AND component_kind=?",
            (event_id, COMMIT_KIND),
        ).fetchone()
        if aggregate is None:
            raise HostedMutationJournalError("complete canonical evidence is unavailable")
        payload = _bound_payload(
            _TERMINAL_DOMAIN,
            {
                "schema": _TERMINAL_SCHEMA,
                "owner": OWNER,
                "descriptor_sha256": prepared.descriptor_sha256,
                "commit_component_sha256": str(aggregate[0]),
                "terminal_sha256": terminal_sha256,
                "disposition": disposition,
            },
            attempt_secret,
        )
        encoded = canonical_json(payload)
        component_digest = digest(payload)
        existing = connection.execute(
            "SELECT value_json, value_hash FROM governance_operation_components "
            "WHERE event_id=? AND component_kind=?",
            (event_id, TERMINAL_KIND),
        ).fetchone()
        if existing is None:
            _insert_component(
                connection,
                event_id=event_id,
                phase="final",
                ordinal=len(prepared.descriptor["required_children"]) + 2,
                kind=TERMINAL_KIND,
                key=prepared.descriptor_sha256,
                payload=payload,
            )
            connection.execute(
                "UPDATE governance_operation_journals SET phase='closed', updated_at=? "
                "WHERE event_id=? AND phase='pending'",
                (now, event_id),
            )
        elif existing != (encoded, component_digest):
            raise HostedMutationJournalError("hosted mutation terminal evidence changed")
        if owns_transaction:
            connection.commit()
        return component_digest
    except BaseException:
        if owns_transaction:
            connection.rollback()
        raise


def retained_dependency_pins(connection: sqlite3.Connection) -> dict[str, frozenset[object]]:
    """Return all non-retiring writer dependencies; malformed evidence blocks GC."""

    pins: dict[str, set[object]] = {key: set() for key in _DEPENDENCY_KEYS}
    rows = connection.execute(
        "SELECT j.event_id, c.component_key, c.value_json, c.value_hash "
        "FROM governance_operation_journals j JOIN governance_operation_components c "
        "ON c.event_id=j.event_id WHERE j.operation=? AND j.principal_id=? "
        "AND c.component_kind=?",
        (OPERATION, OWNER, PLAN_KIND),
    ).fetchall()
    for _event_id_value, component_key, encoded, row_digest in rows:
        if _component_hash(str(encoded)) != row_digest:
            raise HostedMutationJournalError("retained hosted mutation plan changed")
        try:
            payload = json.loads(str(encoded))
            dependencies = _dependencies(payload["dependencies"])
            if payload.get("schema") != _PLAN_SCHEMA:
                raise KeyError("schema")
            if _sha(canonical_json(dependencies)) != payload.get("dependency_manifest_sha256"):
                raise KeyError("dependency digest")
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            raise HostedMutationJournalError("retained hosted mutation dependencies are invalid") from None
        pins["component_keys"].add(str(component_key))
        for key, values in dependencies.items():
            pins[key].update(values)
    return {key: frozenset(values) for key, values in pins.items()}


def validate_recovery_journal_shape(
    connection: sqlite3.Connection, event_id: str
) -> bool:
    """Classify the internal writer variant without claiming secret verification."""

    try:
        journal = connection.execute(
            "SELECT operation, principal_id, phase, prepared_digest FROM governance_operation_journals "
            "WHERE event_id=?",
            (event_id,),
        ).fetchone()
        if (
            journal is None
            or journal[0] != OPERATION
            or journal[1] != OWNER
            or journal[2] not in {"allocating", "pending", "closed"}
        ):
            return False
        rows = connection.execute(
            "SELECT phase, ordinal, component_kind, component_key, value_json, value_hash, status "
            "FROM governance_operation_components WHERE event_id=? ORDER BY ordinal",
            (event_id,),
        ).fetchall()
        if not rows or rows[0][0:4] != (
            "prepared",
            0,
            PLAN_KIND,
            journal[3],
        ):
            return False
        for phase, ordinal, kind, _key, encoded, row_digest, status in rows:
            if (
                status != "complete"
                or kind not in {PLAN_KIND, CHILD_KIND, COMMIT_KIND, TERMINAL_KIND}
                or _component_hash(str(encoded)) != row_digest
                or not isinstance(ordinal, int)
                or phase not in {"prepared", "committed", "final"}
            ):
                return False
        plan = json.loads(str(rows[0][4]))
        if (
            not isinstance(plan, dict)
            or plan.get("schema") != _PLAN_SCHEMA
            or plan.get("owner") != OWNER
            or plan.get("descriptor_sha256") != journal[3]
        ):
            return False
        dependencies = _dependencies(plan.get("dependencies", {}))
        if _sha(canonical_json(dependencies)) != plan.get("dependency_manifest_sha256"):
            return False
        children = plan.get("required_children")
        if not isinstance(children, list):
            return False
        required_ids = [child.get("id") for child in children if isinstance(child, dict)]
        if len(required_ids) != len(children) or len(set(required_ids)) != len(required_ids):
            return False
        child_rows = [row for row in rows if row[2] == CHILD_KIND]
        if [row[3] for row in child_rows] != required_ids[: len(child_rows)]:
            return False
        for row in child_rows:
            component = json.loads(str(row[4]))
            if not isinstance(component, dict):
                return False
            if component.get("child_kind") == "sidecar":
                if (
                    component.get("publication") is not None
                    or not isinstance(component.get("completion_receipt_sha256"), str)
                ):
                    return False
            else:
                publication = component.get("publication")
                if _actual_publication(connection, publication) is None:
                    return False
        commits = [row for row in rows if row[2] == COMMIT_KIND]
        if len(commits) > 1 or (commits and len(child_rows) != len(required_ids)):
            return False
        return True
    except (
        HostedMutationJournalError,
        KeyError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
        sqlite3.Error,
    ):
        return False


def _require_owner_only_database(path: Path) -> None:
    if os.name == "nt":
        if not path.is_absolute():
            raise HostedMutationJournalError("private database path is not trusted")
        return
    try:
        parent = path.parent.lstat()
        info = path.lstat()
    except OSError:
        raise HostedMutationJournalError("private database path is unavailable") from None
    owner = os.geteuid()
    if (
        not path.is_absolute()
        or stat.S_ISLNK(parent.st_mode)
        or not stat.S_ISDIR(parent.st_mode)
        or parent.st_uid != owner
        or stat.S_IMODE(parent.st_mode) & 0o077
        or stat.S_ISLNK(info.st_mode)
        or not stat.S_ISREG(info.st_mode)
        or info.st_uid != owner
        or stat.S_IMODE(info.st_mode) & 0o077
    ):
        raise HostedMutationJournalError("private database path is not owner-only")


def _readonly_connection(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(
        f"{path.as_uri()}?mode=ro", uri=True, timeout=0, isolation_level=None
    )
    connection.execute("PRAGMA query_only=ON")
    connection.execute("PRAGMA busy_timeout=0")
    return connection


def _load_private_recovery_readonly(
    path: Path, descriptor_sha256: str
) -> tuple[PreparedCanonicalMutationRecovery, bytes]:
    _require_owner_only_database(path)
    connection = _readonly_connection(path)
    try:
        header = connection.execute(
            "SELECT length(CAST(prepared_recovery_json AS BLOB)), length(commit_secret) "
            "FROM mutations WHERE prepared_recovery_digest=?",
            (descriptor_sha256,),
        ).fetchone()
        if (
            header is None
            or type(header[0]) is not int
            or not 1 <= header[0] <= hosted_mutation_recovery.MAX_CANONICAL_BYTES
            or type(header[1]) is not int
            or not 1 <= header[1] <= 8192
        ):
            raise HostedMutationJournalError("private prepared recovery is unavailable")
        row = connection.execute(
            "SELECT prepared_recovery_json, commit_secret, digest, attempt_id, commit_token "
            "FROM mutations WHERE prepared_recovery_digest=?",
            (descriptor_sha256,),
        ).fetchone()
    except sqlite3.Error:
        raise HostedMutationJournalError("private prepared recovery is unavailable") from None
    finally:
        connection.close()
    if (
        row is None
        or not isinstance(row[0], str)
        or not isinstance(row[1], bytes)
        or len(row[1]) != 32
    ):
        # Enveloped platform secrets require their existing platform protector;
        # raw or request-supplied fallback material is never accepted here.
        raise HostedMutationJournalError("private prepared recovery secret is unavailable")
    recovery = prepared_recovery_from_json(row[0])
    recovery = validate_prepared_recovery(recovery, attempt_secret=row[1])
    if (
        recovery.descriptor_sha256 != descriptor_sha256
        or recovery.descriptor["command_digest"] != row[2]
        or recovery.descriptor["attempt_id"] != row[3]
        or recovery.descriptor["commit_token"] != row[4]
    ):
        raise HostedMutationJournalError("private prepared recovery identity changed")
    return recovery, row[1]


def _parse_component(encoded: object, expected_hash: object) -> dict[str, object]:
    if not isinstance(encoded, str) or not isinstance(expected_hash, str):
        raise HostedMutationJournalError("journal component is malformed")
    if _component_hash(encoded) != expected_hash:
        raise HostedMutationJournalError("journal component changed")
    value = json.loads(encoded)
    if not isinstance(value, dict):
        raise HostedMutationJournalError("journal component is malformed")
    return value


_CHILD_FIELDS = frozenset(
    {
        "schema",
        "owner",
        "descriptor_sha256",
        "attempt_id",
        "commit_token",
        "child_id",
        "child_kind",
        "plan_sha256",
        "prepared_result_sha256",
        "publication",
        "completion_receipt_sha256",
    }
)
_COMMIT_FIELDS = frozenset(
    {
        "schema",
        "owner",
        "descriptor_sha256",
        "attempt_id",
        "commit_token",
        "children",
        "final_publication",
    }
)


def verify_sidecar_prerequisites(
    connection: sqlite3.Connection,
    *,
    recovery: PreparedCanonicalMutationRecovery,
    child_id: str,
    attempt_secret: bytes,
) -> None:
    """Authenticate every required child before a recoverer writes its sidecar."""

    prepared = validate_prepared_recovery(recovery, attempt_secret=attempt_secret)
    event_id = _event_id(prepared.descriptor_sha256)
    plan = _load_plan(connection, event_id=event_id, attempt_secret=attempt_secret)
    _index, sidecar = _child_manifest(plan, child_id)
    if sidecar.get("kind") != "sidecar":
        raise HostedMutationJournalError("completion receipt child is not a sidecar")
    for required_id in sidecar.get("requires", []):
        row = connection.execute(
            "SELECT value_json, value_hash FROM governance_operation_components "
            "WHERE event_id=? AND component_kind=? AND component_key=?",
            (event_id, CHILD_KIND, required_id),
        ).fetchone()
        if row is None:
            raise HostedMutationJournalError(
                "hosted mutation child prerequisite is absent"
            )
        component = _parse_component(row[0], row[1])
        unsigned = _verify_bound_payload(
            _CHILD_DOMAIN,
            component,
            fields=_CHILD_FIELDS,
            secret=attempt_secret,
        )
        _required_index, required = _child_manifest(plan, str(required_id))
        result = prepared.prepared_results[str(required_id)]
        if (
            unsigned["schema"] != _CHILD_SCHEMA
            or unsigned["owner"] != OWNER
            or unsigned["descriptor_sha256"] != prepared.descriptor_sha256
            or unsigned["attempt_id"] != prepared.descriptor["attempt_id"]
            or unsigned["commit_token"] != prepared.descriptor["commit_token"]
            or unsigned["child_id"] != required_id
            or unsigned["child_kind"] != required["kind"]
            or unsigned["plan_sha256"] != required["plan_sha256"]
            or unsigned["prepared_result_sha256"] != _sha(canonical_json(result))
        ):
            raise HostedMutationJournalError(
                "hosted mutation child prerequisite changed"
            )
        if required["kind"] in {"catalog", "policy"}:
            if unsigned["completion_receipt_sha256"] is not None:
                raise HostedMutationJournalError(
                    "hosted mutation child prerequisite changed"
                )
            _actual_publication(connection, unsigned["publication"])
        elif (
            unsigned["publication"] is not None
            or not isinstance(unsigned["completion_receipt_sha256"], str)
        ):
            raise HostedMutationJournalError(
                "hosted mutation child prerequisite changed"
            )


def _actual_publication(
    connection: sqlite3.Connection, value: object
) -> PublicationTupleEvidence:
    if not isinstance(value, dict) or set(value) != set(_PUBLICATION_FIELDS):
        raise HostedMutationJournalError("publication evidence is malformed")
    event_id = value.get("event_id")
    if not isinstance(event_id, str):
        raise HostedMutationJournalError("publication evidence is malformed")
    actual = _publication(connection, event_id)
    if actual != value:
        raise HostedMutationJournalError("actual committed publication changed")
    return PublicationTupleEvidence(
        event_id=event_id,
        publication_kind=str(value["publication_kind"]),
        predecessor_activation_state_digest=str(
            value["predecessor_activation_state_digest"]
        ),
        target_activation_state_digest=str(value["target_activation_state_digest"]),
        policy_generation_id=str(value["policy_generation_id"]),
        policy_fingerprint=str(value["policy_fingerprint"]),
        projector_schema_version=int(value["projector_schema_version"]),
        catalog_generation=int(value["catalog_generation"]),
        activation_epoch=int(value["activation_epoch"]),
        activated_at=int(value["activated_at"]),
    )


def read_committed_publication_evidence(
    *,
    governance_db_path: Path,
    idempotency_db_path: Path,
    selector: PublicationSelectorEvidence,
) -> CommittedPublicationEvidence | None:
    """Read exact content-free proof without opening stores or acquiring fences."""

    if not isinstance(selector, PublicationSelectorEvidence):
        raise HostedMutationJournalError("publication selector is invalid")
    recovery, secret = _load_private_recovery_readonly(
        Path(idempotency_db_path), selector.descriptor_sha256
    )
    governance_path = Path(governance_db_path)
    _require_owner_only_database(governance_path)
    connection = _readonly_connection(governance_path)
    try:
        event_id = _event_id(recovery.descriptor_sha256)
        journal = connection.execute(
            "SELECT operation, principal_id, phase FROM governance_operation_journals WHERE event_id=?",
            (event_id,),
        ).fetchone()
        if journal is None:
            return None
        if journal[0] != OPERATION or journal[1] != OWNER or journal[2] not in {
            "pending",
            "closed",
        }:
            raise HostedMutationJournalError("hosted mutation journal is not committed")
        plan = _load_plan(connection, event_id=event_id, attempt_secret=secret)
        if (
            plan["descriptor_sha256"] != recovery.descriptor_sha256
            or plan["private_payload_sha256"] != _sha(recovery.to_json())
        ):
            raise HostedMutationJournalError("hosted mutation plan changed")
        kind = CHILD_KIND if selector.child_id is not None else COMMIT_KIND
        row = connection.execute(
            "SELECT value_json, value_hash FROM governance_operation_components "
            "WHERE event_id=? AND component_kind=? AND component_key=?",
            (
                event_id,
                kind,
                selector.child_id
                if selector.child_id is not None
                else recovery.descriptor_sha256,
            ),
        ).fetchone()
        if row is None:
            return None
        component = _parse_component(row[0], row[1])
        if selector.child_id is not None:
            unsigned = _verify_bound_payload(
                _CHILD_DOMAIN, component, fields=_CHILD_FIELDS, secret=secret
            )
            _index, child = _child_manifest(plan, selector.child_id)
            result = recovery.prepared_results[selector.child_id]
            if (
                unsigned["schema"] != _CHILD_SCHEMA
                or unsigned["owner"] != OWNER
                or unsigned["descriptor_sha256"] != recovery.descriptor_sha256
                or unsigned["attempt_id"] != recovery.descriptor["attempt_id"]
                or unsigned["commit_token"] != recovery.descriptor["commit_token"]
                or unsigned["child_kind"] != child["kind"]
                or unsigned["plan_sha256"] != child["plan_sha256"]
                or unsigned["prepared_result_sha256"] != _sha(canonical_json(result))
                or unsigned["completion_receipt_sha256"] is not None
            ):
                raise HostedMutationJournalError("hosted mutation child evidence changed")
            publication_value = unsigned["publication"]
        else:
            unsigned = _verify_bound_payload(
                _COMMIT_DOMAIN, component, fields=_COMMIT_FIELDS, secret=secret
            )
            child_rows = connection.execute(
                "SELECT component_key, value_hash FROM governance_operation_components "
                "WHERE event_id=? AND component_kind=? ORDER BY ordinal",
                (event_id, CHILD_KIND),
            ).fetchall()
            expected_children = [
                {"child_id": str(child_id), "component_sha256": str(component_hash)}
                for child_id, component_hash in child_rows
            ]
            required_ids = [child["id"] for child in recovery.descriptor["required_children"]]
            if (
                unsigned["schema"] != _COMMIT_SCHEMA
                or unsigned["owner"] != OWNER
                or unsigned["descriptor_sha256"] != recovery.descriptor_sha256
                or unsigned["attempt_id"] != recovery.descriptor["attempt_id"]
                or unsigned["commit_token"] != recovery.descriptor["commit_token"]
                or [child_id for child_id, _hash in child_rows] != required_ids
                or unsigned["children"] != expected_children
            ):
                raise HostedMutationJournalError("hosted mutation aggregate evidence changed")
            publication_value = unsigned["final_publication"]
        publication = _actual_publication(connection, publication_value)
    except sqlite3.Error:
        raise HostedMutationJournalError("committed publication evidence is unavailable") from None
    finally:
        connection.close()
    identity = recovery.descriptor
    return CommittedPublicationEvidence(
        component_kind=kind,
        component_sha256=str(row[1]),
        descriptor_sha256=recovery.descriptor_sha256,
        child_id=selector.child_id,
        cell_id=str(identity["cell_id"]),
        logical_vault_id=str(identity["logical_vault_id"]),
        registry_attachment_id=str(identity["registry_attachment_id"]),
        attachment_epoch=int(identity["attachment_epoch"]),
        activation_store_id=str(identity["activation_store_id"]),
        publication=publication,
    )


def _public_activation_tuple(value: object) -> dict[str, object]:
    fields = {"activation_store_id", "activation_epoch", "activation_state_digest"}
    if not isinstance(value, dict) or set(value) != fields:
        raise HostedMutationJournalError("publication selector is invalid")
    store_id = value["activation_store_id"]
    epoch = value["activation_epoch"]
    state_digest = value["activation_state_digest"]
    if (
        not isinstance(store_id, str)
        or not store_id
        or len(store_id.encode("utf-8")) > 512
        or isinstance(epoch, bool)
        or not isinstance(epoch, int)
        or not 1 <= epoch <= 9223372036854775807
        or not isinstance(state_digest, str)
        or len(state_digest) != 64
        or any(character not in "0123456789abcdef" for character in state_digest)
    ):
        raise HostedMutationJournalError("publication selector is invalid")
    return dict(value)


def read_committed_publication_evidence_for_selector(
    *,
    governance_db_path: Path,
    idempotency_db_path: Path,
    selector: Mapping[str, object],
) -> CommittedPublicationEvidence | None:
    """Resolve a public publication tuple to its authenticated private component."""

    if not isinstance(selector, Mapping) or set(selector) != {
        "publication_event_id",
        "predecessor",
        "successor",
    }:
        raise HostedMutationJournalError("publication selector is invalid")
    publication_event_id = selector["publication_event_id"]
    if (
        not isinstance(publication_event_id, str)
        or not publication_event_id
        or len(publication_event_id.encode("utf-8")) > 512
    ):
        raise HostedMutationJournalError("publication selector is invalid")
    predecessor = _public_activation_tuple(selector["predecessor"])
    successor = _public_activation_tuple(selector["successor"])
    if (
        predecessor["activation_store_id"] != successor["activation_store_id"]
        or successor["activation_epoch"] != predecessor["activation_epoch"] + 1
    ):
        raise HostedMutationJournalError("publication selector is invalid")
    governance_path = Path(governance_db_path)
    _require_owner_only_database(governance_path)
    connection = _readonly_connection(governance_path)
    try:
        publication = _publication(connection, publication_event_id)
        if (
            publication["predecessor_activation_state_digest"]
            != predecessor["activation_state_digest"]
            or publication["target_activation_state_digest"]
            != successor["activation_state_digest"]
            or publication["activation_epoch"] != successor["activation_epoch"]
        ):
            raise HostedMutationJournalError("publication selector changed")
        headers = connection.execute(
            "SELECT c.event_id, c.phase, c.ordinal, c.component_kind, c.component_key, "
            "length(CAST(c.value_json AS BLOB)) "
            "FROM governance_operation_components c "
            "JOIN governance_operation_journals j ON j.event_id=c.event_id "
            "WHERE j.operation=? AND j.principal_id=? "
            "AND c.component_kind IN (?, ?) AND instr(c.value_json, ?) > 0 ORDER BY "
            "CASE c.component_kind WHEN ? THEN 0 ELSE 1 END, c.event_id, c.ordinal "
            "LIMIT 258",
            (
                OPERATION,
                OWNER,
                CHILD_KIND,
                COMMIT_KIND,
                f'"event_id":{canonical_json(publication_event_id)}',
                COMMIT_KIND,
            ),
        ).fetchall()
        if len(headers) > 257 or any(
            type(row[5]) is not int or not 1 <= row[5] <= 65536 for row in headers
        ):
            raise HostedMutationJournalError("publication component inventory is invalid")
        candidates: list[tuple[str, str | None]] = []
        for event_id, phase, ordinal, kind, component_key, _length in headers:
            row = connection.execute(
                "SELECT value_json, value_hash FROM governance_operation_components "
                "WHERE event_id=? AND phase=? AND ordinal=?",
                (event_id, phase, ordinal),
            ).fetchone()
            if row is None:
                raise HostedMutationJournalError("publication component changed")
            component = _parse_component(row[0], row[1])
            publication_value = component.get(
                "final_publication" if kind == COMMIT_KIND else "publication"
            )
            if (
                isinstance(publication_value, dict)
                and publication_value.get("event_id") == publication_event_id
            ):
                candidates.append(
                    (
                        str(component.get("descriptor_sha256", "")),
                        None if kind == COMMIT_KIND else str(component_key),
                    )
                )
        if not candidates:
            return None
        descriptor_sha256, child_id = candidates[0]
    except sqlite3.Error:
        raise HostedMutationJournalError("committed publication evidence is unavailable") from None
    finally:
        connection.close()
    evidence = read_committed_publication_evidence(
        governance_db_path=governance_path,
        idempotency_db_path=Path(idempotency_db_path),
        selector=PublicationSelectorEvidence(
            descriptor_sha256=descriptor_sha256,
            child_id=child_id,
        ),
    )
    if evidence is None:
        return None
    if (
        evidence.activation_store_id != successor["activation_store_id"]
        or evidence.publication.event_id != publication_event_id
        or evidence.publication.predecessor_activation_state_digest
        != predecessor["activation_state_digest"]
        or evidence.publication.target_activation_state_digest
        != successor["activation_state_digest"]
        or evidence.publication.activation_epoch != successor["activation_epoch"]
    ):
        raise HostedMutationJournalError("authenticated publication selector changed")
    return evidence


def verify_complete_canonical_evidence(
    recovery: PreparedCanonicalMutationRecovery,
    evidence: CommittedPublicationEvidence | None,
) -> VerifiedCompleteCanonicalEvidence:
    if (
        not isinstance(recovery, PreparedCanonicalMutationRecovery)
        or not isinstance(evidence, CommittedPublicationEvidence)
        or evidence.component_kind != COMMIT_KIND
        or evidence.child_id is not None
        or evidence.descriptor_sha256 != recovery.descriptor_sha256
    ):
        raise HostedMutationJournalError("complete canonical evidence is unavailable")
    return VerifiedCompleteCanonicalEvidence(evidence, _VERIFIED_COMPLETE_TOKEN)


def _render_recipe(node: Mapping[str, object], results: Mapping[str, Mapping[str, object]]) -> Any:
    kind = node["kind"]
    if kind == "literal":
        return node["value"]
    if kind == "object":
        return {name: _render_recipe(child, results) for name, child in node["members"].items()}
    if kind == "list":
        return [_render_recipe(child, results) for child in node["items"]]
    if kind == "child-field":
        try:
            return results[str(node["child_id"])][str(node["field"])]
        except KeyError:
            raise HostedMutationJournalError("prepared result recipe cannot be resolved") from None
    raise HostedMutationJournalError("prepared result recipe is invalid")


def render_verified_result(
    recovery: PreparedCanonicalMutationRecovery,
    evidence: VerifiedCompleteCanonicalEvidence,
    *,
    egress_filter: Callable[[Any], Any],
) -> Any:
    """Render only complete canonical evidence through the current egress policy."""

    if (
        not isinstance(evidence, VerifiedCompleteCanonicalEvidence)
        or evidence._token is not _VERIFIED_COMPLETE_TOKEN
        or evidence.evidence.descriptor_sha256 != recovery.descriptor_sha256
        or not callable(egress_filter)
    ):
        raise HostedMutationJournalError("verified result rendering is unavailable")
    recipe = hosted_mutation_recovery.validate_descriptor(recovery.descriptor)["result_recipe"]
    rendered = _render_recipe(recipe, recovery.prepared_results)
    return egress_filter(rendered)


__all__ = [
    "CHILD_KIND",
    "COMMIT_KIND",
    "CompleteCanonicalEvidence",
    "CommittedPublicationEvidence",
    "HostedMutationJournalError",
    "HostedMutationPlan",
    "OPERATION",
    "OWNER",
    "PLAN_KIND",
    "PreparedCanonicalMutationRecovery",
    "PreparedHostedMutationChild",
    "PublicationSelectorEvidence",
    "PublicationTupleEvidence",
    "TERMINAL_KIND",
    "create_allocating_journal",
    "finalize_complete_evidence",
    "prepare_canonical_mutation_recovery",
    "prepare_sidecar_completion_receipt",
    "prepared_recovery_from_json",
    "record_child_in_transaction",
    "record_sidecar_child",
    "read_committed_publication_evidence",
    "read_committed_publication_evidence_for_selector",
    "record_terminal",
    "render_verified_result",
    "retained_dependency_pins",
    "validate_prepared_recovery",
    "validate_recovery_journal_shape",
    "VerifiedCompleteCanonicalEvidence",
    "verify_complete_canonical_evidence",
    "verify_sidecar_completion_receipt",
    "verify_sidecar_prerequisites",
]
