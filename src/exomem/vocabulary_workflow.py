"""Pure vocabulary consideration contracts; no semantic selection or authority.

Families are registered by product code. Labels inside a supported family remain
open under that family's existing validator. Nothing here imports vault-named
code, writes content, or turns a recorded decision into permission to write.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

REVIEW_PREFIX = "exomem://review/vocabulary/"
PROJECTION_STATUSES = frozenset({"current", "warming", "unavailable"})


@dataclass(frozen=True)
class FamilyDescriptor:
    identifier: str
    evidence_contract: str
    resolution_contract: str
    validator: str
    persistence_owner: str
    allowed_decisions: frozenset[str]
    authority_actions: frozenset[str]
    compatibility: str = "unknown family refuses; existing labels retain owner semantics"

    def __post_init__(self) -> None:
        object.__setattr__(self, "allowed_decisions", frozenset(self.allowed_decisions))
        object.__setattr__(self, "authority_actions", frozenset(self.authority_actions))


class FamilyCatalog:
    """An immutable product catalog, not an authorization or plugin registry."""

    def __init__(self, descriptors: Iterable[FamilyDescriptor]):
        families: dict[str, FamilyDescriptor] = {}
        for descriptor in descriptors:
            if (
                not isinstance(descriptor, FamilyDescriptor)
                or not re.fullmatch(r"[a-z][a-z-]*/v[1-9][0-9]*", descriptor.identifier)
                or descriptor.identifier in families
                or not descriptor.allowed_decisions
                or any("*" in action for action in descriptor.authority_actions)
                or not all(
                    (
                        descriptor.evidence_contract,
                        descriptor.resolution_contract,
                        descriptor.validator,
                        descriptor.persistence_owner,
                        descriptor.compatibility,
                    )
                )
            ):
                raise ValueError("VOCABULARY_FAMILY_INVALID: invalid product descriptor")
            families[descriptor.identifier] = descriptor
        self.families = MappingProxyType(families)

    def get(self, identifier: str) -> FamilyDescriptor:
        try:
            return self.families[identifier]
        except (KeyError, TypeError) as exc:
            raise ValueError("VOCABULARY_FAMILY_UNSUPPORTED: refresh supported families") from exc

    def covered_actions(self, identifier: str, explicit_actions: Iterable[str]) -> frozenset[str]:
        """Describe exact action overlap only; this does not evaluate a grant."""
        actions = frozenset(explicit_actions)
        if any(not isinstance(action, str) or "*" in action for action in actions):
            raise ValueError("VOCABULARY_ACTION_INVALID: actions must be explicit")
        return self.get(identifier).authority_actions & actions


_CATALOG = FamilyCatalog(
    (
        FamilyDescriptor(
            "entity-instance/v1",
            "entity-lifecycle/v1",
            "connect_memory.resolve",
            "entity_schema",
            "entity_writer",
            frozenset({"reuse", "enrich", "propose-new", "defer"}),
            frozenset({"entity.create"}),
        ),
        FamilyDescriptor(
            "entity-type/v1",
            "entity-type-registry/v1",
            "schema_memory.resolve-entity-type",
            "entity_types.validate_proposal",
            "entity_types",
            frozenset({"reuse", "propose-new", "defer"}),
            frozenset({"entity_type.add"}),
        ),
        FamilyDescriptor(
            "relation-type/v1",
            "relation-vocabulary/v1",
            "connect_memory.resolve-relation",
            "relation_registry.validate_proposal",
            "relation_registry",
            frozenset({"reuse", "propose-new", "generic", "no-edge", "defer"}),
            frozenset({"relation_type.add", "edge.add"}),
        ),
    )
)
FAMILIES = _CATALOG.families

_CHOICE_CONTRACTS = MappingProxyType(
    {
        "entity-instance/v1": {
            "reuse": {"choice": {"required": ["canonical"]}},
            "enrich": {"choice": {"required": ["canonical"]}},
            "propose-new": {
                "choice": {"required": ["canonical", "definition"]},
                "canonical": "equals definition.name",
                "definition": {
                    "type": "object",
                    "required": ["entity_type", "name", "summary"],
                    "additional_properties": False,
                    "properties": {
                        "entity_type": {
                            "type": "string",
                            "pattern": "[a-z][a-z0-9-]*",
                            "semantics": "current active canonical entity type",
                        },
                        "name": {"type": "string", "minLength": 1},
                        "summary": {"type": "string", "minLength": 1},
                    },
                },
            },
            "defer": {"choice": None},
        },
        "entity-type/v1": {
            "reuse": {"choice": {"required": ["canonical"]}},
            "propose-new": {
                "choice": {"required": ["canonical", "definition"]},
                "canonical": "registry key for definition",
                "definition": {
                    "validator": "entity_types.validate_proposal",
                    "proposal_entry": "entity_types[choice.canonical]",
                    "include_proposal_wrapper": False,
                },
            },
            "defer": {"choice": None},
        },
        "relation-type/v1": {
            "reuse": {"choice": {"required": ["canonical"]}},
            "propose-new": {
                "choice": {"required": ["canonical", "definition"]},
                "canonical": "registry key for definition",
                "definition": {
                    "validator": "relation_registry.validate_proposal",
                    "proposal_entry": "extensions[choice.canonical]",
                    "include_proposal_wrapper": False,
                },
            },
            "generic": {"choice": None},
            "no-edge": {"choice": None},
            "defer": {"choice": None},
        },
    }
)


def family_descriptor(identifier: str) -> FamilyDescriptor:
    return _CATALOG.get(identifier)


def choice_contract(identifier: str) -> dict[str, Any]:
    """Return the public, family-specific shape for a recorded decision."""
    family_descriptor(identifier)
    return json.loads(json.dumps(_CHOICE_CONTRACTS[identifier]))


def _hash(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _versions(value: Any) -> tuple[tuple[str, str], ...]:
    if not isinstance(value, Mapping) or not all(
        _text(key) and _text(version) for key, version in value.items()
    ):
        raise ValueError("VOCABULARY_DECISION_INVALID: versions must be nonempty string pairs")
    return tuple(sorted(value.items()))


@dataclass(frozen=True, order=True)
class Evidence:
    ref: str
    version: str
    origin: str | None = None

    def __post_init__(self) -> None:
        if (
            not _text(self.ref)
            or not _text(self.version)
            or (self.origin is not None and not _text(self.origin))
        ):
            raise ValueError("VOCABULARY_EVIDENCE_INVALID: reference and content version required")

    def to_dict(self) -> dict[str, Any]:
        return {"ref": self.ref, "version": self.version, "origin": self.origin}


@dataclass(frozen=True)
class WorkItem:
    ref: str
    family: str
    signal: str
    fingerprint: str
    target_versions: tuple[tuple[str, str], ...]
    evidence: tuple[Evidence, ...]
    registry_hashes: tuple[tuple[str, str], ...]
    projection_status: str
    continuation: str | None = None
    paths: tuple[tuple[str, str], ...] = ()
    question: str | None = None
    logical_identity: str | None = None
    projection_currency: tuple[tuple[str, str], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        result = {
            "ref": self.ref,
            "family": self.family,
            "signal": self.signal,
            "fingerprint": self.fingerprint,
            "target_versions": dict(self.target_versions),
            "evidence": [value.to_dict() for value in self.evidence],
            "registry_hashes": dict(self.registry_hashes),
            "projection": {
                "status": self.projection_status,
                "currency": dict(self.projection_currency),
            },
            "supported_decisions": sorted(family_descriptor(self.family).allowed_decisions),
            "continuation": self.continuation,
            "paths": dict(self.paths),
        }
        if self.question is not None:
            result["question"] = self.question
        if self.logical_identity is not None:
            result["logical_identity"] = self.logical_identity
        return result


def make_item(
    *,
    family: str,
    signal: str,
    targets: Mapping[str, str],
    evidence: Iterable[Evidence],
    registry_hashes: Mapping[str, str],
    projection_status: str,
    continuation: str | None = None,
    paths: Mapping[str, str] | None = None,
    question: str | None = None,
    logical_identity: str | None = None,
    projection_currency: Mapping[str, str] | None = None,
) -> WorkItem:
    family_descriptor(family)
    versions = _versions(targets)
    hashes = _versions(registry_hashes)
    if not versions or not _text(signal) or projection_status not in PROJECTION_STATUSES:
        raise ValueError("VOCABULARY_ITEM_INVALID: targets, signal and projection status required")
    if continuation is not None and not _text(continuation):
        raise ValueError("VOCABULARY_ITEM_INVALID: invalid continuation")
    if question is not None and (not _text(question) or len(question.strip()) > 2000):
        raise ValueError("VOCABULARY_ITEM_INVALID: question must be 1 to 2000 characters")
    if logical_identity is not None and (
        not _text(logical_identity) or len(logical_identity.strip()) > 256
    ):
        raise ValueError("VOCABULARY_ITEM_INVALID: logical identity must be 1 to 256 characters")
    entries = tuple(evidence)
    if not all(isinstance(entry, Evidence) for entry in entries):
        raise ValueError("VOCABULARY_EVIDENCE_INVALID: typed evidence required")
    entries = tuple(
        sorted(set(entries), key=lambda entry: (entry.ref, entry.version, entry.origin or ""))
    )
    identity = {
        "family": family,
        "signal": signal,
        "logical_identity": logical_identity.strip()
        if logical_identity is not None
        else {"targets": [ref for ref, _ in versions]},
    }
    if question is not None:
        question = question.strip()
        identity["question"] = question
    currency = _versions(projection_currency or {})
    fingerprint = _hash(
        {
            **identity,
            "target_versions": versions,
            "evidence": [entry.to_dict() for entry in entries],
            "projection_currency": currency,
        }
    )
    path_hints = _versions(paths or {})
    if not set(dict(path_hints)) <= {
        *(ref for ref, _ in versions),
        *(entry.ref for entry in entries),
    }:
        raise ValueError("VOCABULARY_ITEM_INVALID: path hints must name the item's references")
    return WorkItem(
        REVIEW_PREFIX + _hash(identity)[:24],
        family,
        signal,
        fingerprint,
        versions,
        entries,
        hashes,
        projection_status,
        continuation,
        path_hints,
        question,
        logical_identity.strip() if logical_identity is not None else None,
        currency,
    )


@dataclass(frozen=True)
class VocabularyDecision:
    item_ref: str
    fingerprint: str
    registry_hashes: tuple[tuple[str, str], ...]
    target_versions: tuple[tuple[str, str], ...]
    outcome: str
    rationale: str
    family: str
    choice_json: str | None

    @property
    def initial_state(self) -> str:
        if self.outcome == "defer":
            return "deferred"
        if self.outcome in {"generic", "no-edge"}:
            return "resolved_without_mutation"
        return "proposed"

    def to_dict(self) -> dict[str, Any]:
        return {
            "item_ref": self.item_ref,
            "fingerprint": self.fingerprint,
            "registry_hashes": dict(self.registry_hashes),
            "target_versions": dict(self.target_versions),
            "outcome": self.outcome,
            "rationale": self.rationale,
            "family": self.family,
            "choice": json.loads(self.choice_json) if self.choice_json is not None else None,
        }


def validation_feedback(findings: list[dict[str, str]]) -> str:
    """Expose bounded canonical validation feedback for a submitted definition."""
    return "; ".join(
        f"{str(finding.get('path', finding.get('span', 'definition')))[:160]}: "
        f"{str(finding.get('detail', finding.get('code', 'invalid')))[:240]}"
        for finding in findings[:4]
    )


def _requires_live_registry(
    family: str,
    definition: dict,
    finding: dict[str, str],
) -> bool:
    # A definition can refer to an existing extension outside this one-entry
    # shape check. Public decision persistence validates the merged live registry
    # in vocabulary_review._validate_live_choice. Defer only missing-reference
    # findings; status constraints, malformed fields and cycles still fail here.
    code, detail = finding.get("code"), finding.get("detail")
    reference = definition.get(str(finding.get("path", "")).rsplit(".", 1)[-1])
    if not isinstance(reference, str) or not reference.strip():
        return False
    if family == "relation-type/v1":
        from . import relation_registry

        return (
            code in {"invalid_inverse", "invalid_replacement"}
            and (detail == "must resolve to a canonical relation")
            and bool(relation_registry._KEY_RE.fullmatch(reference))
        )
    from . import entity_types

    return (
        family == "entity-type/v1"
        and code == "invalid_replacement"
        and (detail == "must name a registered entity type")
        and bool(entity_types._ID_RE.fullmatch(reference))
    )


def _choice(family: str, outcome: str, value: Any) -> str | None:
    if outcome in {"generic", "no-edge", "defer"}:
        if value is not None:
            raise ValueError("VOCABULARY_DECISION_INVALID: abstention has no selected meaning")
        return None
    expected = {"canonical", "definition"} if outcome == "propose-new" else {"canonical"}
    if (
        not isinstance(value, Mapping)
        or set(value) != expected
        or not _text(value.get("canonical"))
    ):
        raise ValueError("VOCABULARY_DECISION_INVALID: name the canonical choice or proposal")
    canonical = value["canonical"]
    if len(canonical) > 256:
        raise ValueError("VOCABULARY_DECISION_INVALID: canonical choice is too long")
    if outcome == "propose-new":
        definition = value["definition"]
        if not isinstance(definition, dict):
            raise ValueError("VOCABULARY_DECISION_INVALID: a typed definition is required")
        if family == "relation-type/v1":
            from . import relation_registry

            findings = relation_registry.validate_proposal(
                {"schema_version": 1, "extensions": {canonical: definition}}
            )
        elif family == "entity-type/v1":
            from . import entity_types

            findings = entity_types.validate_proposal(
                {"schema_version": 1, "entity_types": {canonical: definition}}
            )
        else:
            from . import link

            required_fields = {"entity_type", "name", "summary"}
            unsupported = sorted(set(definition) - required_fields)
            if unsupported:
                raise ValueError(
                    "VOCABULARY_DECISION_INVALID: entity proposal definition has unsupported "
                    f"fields: {', '.join(unsupported)}"
                )
            missing = sorted(required_fields - set(definition))
            if missing:
                raise ValueError(
                    "VOCABULARY_DECISION_INVALID: entity proposal definition is missing required "
                    f"fields: {', '.join(missing)}"
                )
            if not isinstance(definition["entity_type"], str) or not re.fullmatch(
                r"[a-z][a-z0-9-]*", definition["entity_type"]
            ):
                raise ValueError(
                    "VOCABULARY_DECISION_INVALID: entity proposal entity_type must be a lowercase "
                    "canonical type"
                )
            if not _text(definition["name"]) or not _text(definition["summary"]):
                raise ValueError(
                    "VOCABULARY_DECISION_INVALID: entity proposal name and summary must be "
                    "nonempty strings"
                )
            if canonical != definition["name"]:
                raise ValueError(
                    "VOCABULARY_DECISION_INVALID: canonical choice must equal entity proposal name"
                )
            findings = link._validate(**definition, decision_status=None)
        findings = [
            finding
            for finding in (findings or [])
            if not _requires_live_registry(family, definition, finding)
        ]
        if findings:
            raise ValueError("VOCABULARY_DECISION_INVALID: " + validation_feedback(findings))
    try:
        return json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("VOCABULARY_DECISION_INVALID: choice must be JSON-shaped") from exc


def validate_decision(item: WorkItem, payload: Mapping[str, Any]) -> VocabularyDecision:
    """Validate the reviewed snapshot before any disposition or content write."""
    required = {
        "item_ref",
        "fingerprint",
        "registry_hashes",
        "target_versions",
        "outcome",
        "rationale",
        "family",
        "choice",
    }
    if not isinstance(payload, Mapping) or set(payload) != required:
        raise ValueError("VOCABULARY_DECISION_INVALID: use only the typed decision fields")
    hashes = _versions(payload["registry_hashes"])
    versions = _versions(payload["target_versions"])
    outcome = payload["outcome"]
    if (
        not _text(outcome)
        or outcome not in family_descriptor(item.family).allowed_decisions
        or not _text(payload["rationale"])
        or len(payload["rationale"]) > 2000
        or not _text(payload["item_ref"])
        or not _text(payload["fingerprint"])
        or payload["family"] != item.family
    ):
        raise ValueError("VOCABULARY_DECISION_INVALID: unsupported outcome or invalid rationale")
    if (
        payload["item_ref"] != item.ref
        or payload["fingerprint"] != item.fingerprint
        or hashes != item.registry_hashes
        or versions != item.target_versions
    ):
        raise ValueError("VOCABULARY_DECISION_STALE: refresh the item and review current evidence")
    if item.projection_status != "current" and outcome != "defer":
        raise ValueError("VOCABULARY_EVIDENCE_UNAVAILABLE: refresh evidence or defer")
    selected = _choice(item.family, outcome, payload["choice"])
    return VocabularyDecision(
        item.ref,
        item.fingerprint,
        hashes,
        versions,
        outcome,
        payload["rationale"].strip(),
        item.family,
        selected,
    )
