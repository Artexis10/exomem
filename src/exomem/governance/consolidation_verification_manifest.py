"""Immutable owner-only contracts for governed-consolidation probes."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, NoReturn

from .. import reserved_paths
from . import consolidation_plan, consolidation_plan_store, consolidation_verification

VERIFICATION_CONTRACT_SCHEMA = "exomem.consolidation-verification-contract/v1"
VERIFICATION_MANIFEST_SCHEMA = "exomem.consolidation-verification-manifest/v1"

_CONTRACT_DOMAIN = VERIFICATION_CONTRACT_SCHEMA.encode("ascii")
_MANIFEST_DOMAIN = VERIFICATION_MANIFEST_SCHEMA.encode("ascii")
_DESCRIPTOR_ID = "consolidation-tree"
_OWNER = "consolidation.run"
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_UUID4 = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z")
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}\Z")
_COMMAND = re.compile(r"[a-z][a-z0-9_]{0,127}\Z")
_SURFACES = frozenset({"cli", "hosted", "mcp", "rest"})
_FORBIDDEN_ARGUMENT_KEYS = frozenset(
    {
        "authorization_session_credential",
        "authorization_session_id",
        "authority",
        "cell_id",
        "consolidation_authority",
        "issuer",
        "logical_vault_id",
        "principal",
        "principal_scope",
        "purpose",
    }
)
_COMMON_CONTRACT_FIELDS = frozenset(
    {
        "probe_id",
        "executor_id",
        "surface",
        "principal_kind",
        "principal_id",
        "purpose",
        "command_name",
        "arguments",
        "expected_result_digest",
    }
)
_DELEGATED_CONTRACT_FIELDS = _COMMON_CONTRACT_FIELDS | frozenset(
    {"principal_attestation_fingerprint"}
)
_MAX_CONTRACTS = 1024
_MAX_ARGUMENT_BYTES = 1 * 1024 * 1024
_MAX_MANIFEST_BYTES = 16 * 1024 * 1024

__all__ = [
    "VERIFICATION_CONTRACT_SCHEMA",
    "VERIFICATION_MANIFEST_SCHEMA",
    "ConsolidationVerificationManifestStore",
    "ConsolidationVerificationManifestUnavailable",
    "VerificationContract",
    "VerificationManifest",
    "build_verification_manifest",
    "canonical_verification_manifest",
    "contract_arguments",
    "parse_verification_manifest",
]


class ConsolidationVerificationManifestUnavailable(RuntimeError):
    """Content-free refusal for missing, changed, or malformed probe contracts."""

    code = "CONSOLIDATION_VERIFICATION_MANIFEST_UNAVAILABLE"

    def __init__(self) -> None:
        super().__init__(self.code)


@dataclass(frozen=True, slots=True)
class VerificationContract:
    schema: str
    probe_id: str
    probe_kind: Literal["positive", "negative"]
    executor_id: str
    surface: Literal["cli", "hosted", "mcp", "rest"]
    principal_kind: Literal["owner", "delegated"]
    principal_id: str
    principal_attestation_fingerprint: str | None
    purpose: str
    command_name: str
    arguments_bytes: bytes
    expected_result_digest: str
    contract_digest: str


@dataclass(frozen=True, slots=True)
class VerificationManifest:
    schema: str
    positive_contracts: tuple[VerificationContract, ...]
    negative_contracts: tuple[VerificationContract, ...]
    verification_plan: consolidation_verification.VerificationPlan
    digest: str

    @property
    def contracts(self) -> tuple[VerificationContract, ...]:
        return self.positive_contracts + self.negative_contracts


def _fail() -> NoReturn:
    raise ConsolidationVerificationManifestUnavailable from None


def _digest(value: object) -> str:
    if type(value) is not str or _DIGEST.fullmatch(value) is None:
        _fail()
    return value


def _identifier(value: object) -> str:
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        _fail()
    return value


def _framed_digest(domain: bytes, value: object) -> str:
    try:
        encoded = consolidation_plan.canonical_closed_jcs(value)
    except consolidation_plan.ConsolidationPlanUnavailable:
        _fail()
    framed = len(domain).to_bytes(4, "big") + domain + len(encoded).to_bytes(8, "big") + encoded
    return hashlib.sha256(framed).hexdigest()


def _forbidden_argument(value: object) -> bool:
    if isinstance(value, Mapping):
        return any(
            type(key) is not str or key in _FORBIDDEN_ARGUMENT_KEYS or _forbidden_argument(item)
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple)):
        return any(_forbidden_argument(item) for item in value)
    return False


def _arguments_bytes(value: object) -> bytes:
    if not isinstance(value, Mapping) or _forbidden_argument(value):
        _fail()
    try:
        encoded = consolidation_plan.canonical_closed_jcs(value)
    except (RecursionError, consolidation_plan.ConsolidationPlanUnavailable):
        _fail()
    if len(encoded) > _MAX_ARGUMENT_BYTES:
        _fail()
    return encoded


def contract_arguments(contract: VerificationContract) -> dict[str, object]:
    """Return a fresh mutable copy of one exact canonical command argument object."""

    if not isinstance(contract, VerificationContract):
        _fail()
    try:
        parsed = json.loads(contract.arguments_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError):
        _fail()
    if not isinstance(parsed, dict) or _arguments_bytes(parsed) != contract.arguments_bytes:
        _fail()
    return parsed


def _contract_value(contract: VerificationContract) -> dict[str, object]:
    value: dict[str, object] = {
        "schema": contract.schema,
        "probe_id": contract.probe_id,
        "probe_kind": contract.probe_kind,
        "executor_id": contract.executor_id,
        "surface": contract.surface,
        "principal_kind": contract.principal_kind,
        "principal_id": contract.principal_id,
        "purpose": contract.purpose,
        "command_name": contract.command_name,
        "arguments": contract_arguments(contract),
        "expected_result_digest": contract.expected_result_digest,
    }
    if contract.principal_kind == "delegated":
        value["principal_attestation_fingerprint"] = _digest(
            contract.principal_attestation_fingerprint
        )
    elif contract.principal_attestation_fingerprint is not None:
        _fail()
    return value


def _build_contract(
    value: object,
    *,
    probe_kind: Literal["positive", "negative"],
) -> VerificationContract:
    if not isinstance(value, Mapping):
        _fail()
    principal_kind = value.get("principal_kind")
    expected_fields = (
        _COMMON_CONTRACT_FIELDS if principal_kind == "owner" else _DELEGATED_CONTRACT_FIELDS
    )
    if frozenset(value) != expected_fields:
        _fail()
    probe_id = _identifier(value["probe_id"])
    executor_id = _identifier(value["executor_id"])
    if executor_id != consolidation_verification.CANONICAL_SURFACE_EXECUTOR_ID:
        _fail()
    surface = value["surface"]
    if surface not in _SURFACES:
        _fail()
    principal_id = _identifier(value["principal_id"])
    purpose = _identifier(value["purpose"])
    command_name = value["command_name"]
    if type(command_name) is not str or _COMMAND.fullmatch(command_name) is None:
        _fail()
    fingerprint: str | None
    if principal_kind == "owner":
        if principal_id != "owner":
            _fail()
        fingerprint = None
    elif principal_kind == "delegated":
        if principal_id == "owner":
            _fail()
        fingerprint = _digest(value["principal_attestation_fingerprint"])
    else:
        _fail()
    preliminary = VerificationContract(
        schema=VERIFICATION_CONTRACT_SCHEMA,
        probe_id=probe_id,
        probe_kind=probe_kind,
        executor_id=executor_id,
        surface=surface,
        principal_kind=principal_kind,
        principal_id=principal_id,
        principal_attestation_fingerprint=fingerprint,
        purpose=purpose,
        command_name=command_name,
        arguments_bytes=_arguments_bytes(value["arguments"]),
        expected_result_digest=_digest(value["expected_result_digest"]),
        contract_digest="0" * 64,
    )
    return VerificationContract(
        schema=preliminary.schema,
        probe_id=preliminary.probe_id,
        probe_kind=preliminary.probe_kind,
        executor_id=preliminary.executor_id,
        surface=preliminary.surface,
        principal_kind=preliminary.principal_kind,
        principal_id=preliminary.principal_id,
        principal_attestation_fingerprint=preliminary.principal_attestation_fingerprint,
        purpose=preliminary.purpose,
        command_name=preliminary.command_name,
        arguments_bytes=preliminary.arguments_bytes,
        expected_result_digest=preliminary.expected_result_digest,
        contract_digest=_framed_digest(_CONTRACT_DOMAIN, _contract_value(preliminary)),
    )


def _sequence(value: object) -> Sequence[object]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        _fail()
    if not 1 <= len(value) <= _MAX_CONTRACTS:
        _fail()
    return value


def _manifest_value(manifest: VerificationManifest) -> dict[str, object]:
    return {
        "schema": manifest.schema,
        "positive_contracts": [_contract_value(item) for item in manifest.positive_contracts],
        "negative_contracts": [_contract_value(item) for item in manifest.negative_contracts],
    }


def build_verification_manifest(
    *,
    positive_contracts: Sequence[Mapping[str, object]],
    negative_contracts: Sequence[Mapping[str, object]],
) -> VerificationManifest:
    """Build exact owner-protected contracts and their public digest-only plan."""

    positive_values = _sequence(positive_contracts)
    negative_values = _sequence(negative_contracts)
    if len(positive_values) + len(negative_values) > _MAX_CONTRACTS:
        _fail()
    positive = tuple(_build_contract(item, probe_kind="positive") for item in positive_values)
    negative = tuple(_build_contract(item, probe_kind="negative") for item in negative_values)
    contracts = positive + negative
    if len({contract.probe_id for contract in contracts}) != len(contracts):
        _fail()
    try:
        plan = consolidation_verification.build_verification_plan(
            positive_probes=tuple(
                {
                    "probe_id": contract.probe_id,
                    "executor_id": contract.executor_id,
                    "contract_digest": contract.contract_digest,
                    "expected_result_digest": contract.expected_result_digest,
                }
                for contract in positive
            ),
            negative_probes=tuple(
                {
                    "probe_id": contract.probe_id,
                    "executor_id": contract.executor_id,
                    "contract_digest": contract.contract_digest,
                    "expected_result_digest": contract.expected_result_digest,
                }
                for contract in negative
            ),
        )
    except consolidation_verification.ConsolidationVerificationUnavailable:
        _fail()
    preliminary = VerificationManifest(
        schema=VERIFICATION_MANIFEST_SCHEMA,
        positive_contracts=positive,
        negative_contracts=negative,
        verification_plan=plan,
        digest="0" * 64,
    )
    value = _manifest_value(preliminary)
    try:
        raw = consolidation_plan.canonical_closed_jcs(value)
    except consolidation_plan.ConsolidationPlanUnavailable:
        _fail()
    if len(raw) > _MAX_MANIFEST_BYTES:
        _fail()
    return VerificationManifest(
        schema=preliminary.schema,
        positive_contracts=preliminary.positive_contracts,
        negative_contracts=preliminary.negative_contracts,
        verification_plan=preliminary.verification_plan,
        digest=_framed_digest(_MANIFEST_DOMAIN, value),
    )


def canonical_verification_manifest(manifest: VerificationManifest) -> bytes:
    """Return exact canonical bytes for one complete verification manifest."""

    if not isinstance(manifest, VerificationManifest):
        _fail()
    rebuilt = build_verification_manifest(
        positive_contracts=tuple(_contract_input(item) for item in manifest.positive_contracts),
        negative_contracts=tuple(_contract_input(item) for item in manifest.negative_contracts),
    )
    if rebuilt != manifest:
        _fail()
    try:
        return consolidation_plan.canonical_closed_jcs(_manifest_value(rebuilt))
    except consolidation_plan.ConsolidationPlanUnavailable:
        _fail()


def _contract_input(contract: VerificationContract) -> dict[str, object]:
    value = _contract_value(contract)
    value.pop("schema")
    value.pop("probe_kind")
    return value


def parse_verification_manifest(raw: bytes) -> VerificationManifest:
    """Parse only exact canonical verification-manifest bytes."""

    try:
        parsed = consolidation_plan._parse_canonical_mapping(  # noqa: SLF001
            raw,
            maximum=_MAX_MANIFEST_BYTES,
        )
        if frozenset(parsed) != {"schema", "positive_contracts", "negative_contracts"}:
            _fail()
        if parsed["schema"] != VERIFICATION_MANIFEST_SCHEMA:
            _fail()
        positive = parsed["positive_contracts"]
        negative = parsed["negative_contracts"]
        if not isinstance(positive, list) or not isinstance(negative, list):
            _fail()
        manifest = build_verification_manifest(
            positive_contracts=tuple(_parsed_contract_input(item, "positive") for item in positive),
            negative_contracts=tuple(_parsed_contract_input(item, "negative") for item in negative),
        )
        if canonical_verification_manifest(manifest) != raw:
            _fail()
        return manifest
    except ConsolidationVerificationManifestUnavailable:
        raise
    except (
        RecursionError,
        consolidation_plan.ConsolidationPlanUnavailable,
        consolidation_verification.ConsolidationVerificationUnavailable,
    ):
        _fail()


def _parsed_contract_input(value: object, probe_kind: str) -> dict[str, object]:
    if not isinstance(value, Mapping):
        _fail()
    if value.get("schema") != VERIFICATION_CONTRACT_SCHEMA or value.get("probe_kind") != probe_kind:
        _fail()
    result = dict(value)
    result.pop("schema")
    result.pop("probe_kind")
    return result


def _bind_to_stored_plan(
    *,
    run_id: str,
    plan_digest: str,
    plan: object,
    bundle: object,
    manifest: VerificationManifest,
) -> None:
    """Bind exact contracts and require the full approved principal/purpose matrix."""

    preimage = getattr(plan, "preimage", None)
    verification = preimage.get("verification_plan") if isinstance(preimage, Mapping) else None
    if (
        getattr(plan, "digest", None) != plan_digest
        or not isinstance(preimage, Mapping)
        or preimage.get("run_id") != run_id
        or preimage.get("plan_kind") != "cutover"
        or not isinstance(verification, Mapping)
        or frozenset(verification) != {"schema", "positive_probe_digest", "negative_probe_digest"}
        or verification.get("schema") != "exomem.consolidation-verification-plan/v1"
        or verification.get("positive_probe_digest")
        != manifest.verification_plan.positive_probe_digest
        or verification.get("negative_probe_digest")
        != manifest.verification_plan.negative_probe_digest
    ):
        _fail()
    attestations = getattr(bundle, "attestations", None)
    principal_requirements = getattr(bundle, "principal_requirements", None)
    if (
        not isinstance(attestations, Sequence)
        or isinstance(attestations, (str, bytes))
        or not isinstance(principal_requirements, Sequence)
        or isinstance(principal_requirements, (str, bytes))
    ):
        _fail()
    by_fingerprint: dict[str, object] = {}
    by_principal: dict[str, object] = {}
    for attestation in attestations:
        fingerprint = getattr(attestation, "fingerprint", None)
        principal_id = getattr(attestation, "principal_id", None)
        purposes = getattr(attestation, "purposes", None)
        if (
            type(fingerprint) is not str
            or _DIGEST.fullmatch(fingerprint) is None
            or type(principal_id) is not str
            or not isinstance(purposes, Sequence)
            or isinstance(purposes, (str, bytes))
        ):
            _fail()
        checked_principal = _identifier(principal_id)
        checked_purposes = tuple(_identifier(purpose) for purpose in purposes)
        if not checked_purposes or checked_purposes != tuple(sorted(set(checked_purposes))):
            _fail()
        if fingerprint in by_fingerprint or checked_principal in by_principal:
            _fail()
        by_fingerprint[fingerprint] = attestation
        by_principal[checked_principal] = attestation
    required_pairs: set[tuple[str, str]] = set()
    for requirement in principal_requirements:
        if type(requirement) is not tuple or len(requirement) != 2:
            _fail()
        principal_id, purposes = requirement
        if (
            type(principal_id) is not str
            or not isinstance(purposes, Sequence)
            or isinstance(purposes, (str, bytes))
        ):
            _fail()
        checked_principal = _identifier(principal_id)
        checked_purposes = tuple(_identifier(purpose) for purpose in purposes)
        if not checked_purposes or checked_purposes != tuple(sorted(set(checked_purposes))):
            _fail()
        attestation = by_principal.get(checked_principal)
        attested_purposes = tuple(getattr(attestation, "purposes", ()))
        for purpose in checked_purposes:
            if purpose not in attested_purposes:
                _fail()
            pair = (checked_principal, purpose)
            if pair in required_pairs:
                _fail()
            required_pairs.add(pair)
    if set(by_principal) != {principal_id for principal_id, _purpose in required_pairs}:
        _fail()
    for contract in manifest.contracts:
        if contract.principal_kind == "owner":
            continue
        attestation = by_fingerprint.get(contract.principal_attestation_fingerprint or "")
        purposes = getattr(attestation, "purposes", (contract.purpose,))
        if (
            attestation is None
            or getattr(attestation, "principal_id", None) != contract.principal_id
            or getattr(attestation, "surface", contract.surface) != contract.surface
            or contract.purpose not in purposes
        ):
            _fail()
    positive_pairs = {
        (contract.principal_id, contract.purpose)
        for contract in manifest.positive_contracts
        if contract.principal_kind == "delegated"
    }
    negative_pairs = {
        (contract.principal_id, contract.purpose)
        for contract in manifest.negative_contracts
        if contract.principal_kind == "delegated"
    }
    if (
        not any(contract.principal_kind == "owner" for contract in manifest.positive_contracts)
        or not required_pairs <= positive_pairs
        or not required_pairs <= negative_pairs
    ):
        _fail()


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
    except ConsolidationVerificationManifestUnavailable:
        raise
    except (OSError, RuntimeError, ValueError):
        _fail()


class ConsolidationVerificationManifestStore:
    """Persist exact probe contracts beside one immutable cutover plan."""

    def __init__(self, vault_root: Path | str) -> None:
        self.vault_root = Path(vault_root).absolute()
        self.plan_store = consolidation_plan_store.ConsolidationPlanStore(self.vault_root)

    def path(self, run_id: str, plan_digest: str) -> Path:
        if type(run_id) is not str or _UUID4.fullmatch(run_id) is None:
            _fail()
        checked_digest = _digest(plan_digest)
        return (
            self.plan_store._plan_dir(run_id, "cutover", checked_digest)  # noqa: SLF001
            / "verification-manifest.json"
        )

    def _bind(self, run_id: str, plan_digest: str, manifest: VerificationManifest) -> None:
        try:
            plan = self.plan_store.load(
                run_id,
                plan_kind="cutover",
                plan_digest=plan_digest,
            )
            bundle = self.plan_store.load_policy_bundle(
                run_id,
                plan_kind="cutover",
                plan_digest=plan_digest,
            )
        except consolidation_plan_store.ConsolidationPlanStoreUnavailable:
            _fail()
        _bind_to_stored_plan(
            run_id=run_id,
            plan_digest=plan_digest,
            plan=plan,
            bundle=bundle,
            manifest=manifest,
        )

    def persist(
        self,
        run_id: str,
        plan_digest: str,
        manifest: VerificationManifest,
    ) -> VerificationManifest:
        """Create or adopt one byte-identical immutable manifest."""

        raw = canonical_verification_manifest(manifest)
        path = self.path(run_id, plan_digest)
        self._bind(run_id, plan_digest, manifest)
        with _authority(self.vault_root, mutation=True):
            try:
                existing = reserved_paths._read_owner_bytes(  # noqa: SLF001
                    self.vault_root,
                    path,
                    _DESCRIPTOR_ID,
                    limit=_MAX_MANIFEST_BYTES,
                )
            except FileNotFoundError:
                reserved_paths._publish_owner_bytes(  # noqa: SLF001
                    self.vault_root,
                    path,
                    _DESCRIPTOR_ID,
                    raw,
                    require_missing=True,
                )
            else:
                if existing != raw:
                    _fail()
        return manifest

    def load(self, run_id: str, plan_digest: str) -> VerificationManifest:
        """Reload and rebind one exact immutable manifest after restart."""

        path = self.path(run_id, plan_digest)
        with _authority(self.vault_root, mutation=False):
            try:
                raw = reserved_paths._read_owner_bytes(  # noqa: SLF001
                    self.vault_root,
                    path,
                    _DESCRIPTOR_ID,
                    limit=_MAX_MANIFEST_BYTES,
                )
            except FileNotFoundError:
                _fail()
        manifest = parse_verification_manifest(raw)
        self._bind(run_id, plan_digest, manifest)
        return manifest
