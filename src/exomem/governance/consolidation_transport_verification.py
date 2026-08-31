"""Exact-cell transport verification basis and process-local probe route."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Literal, NoReturn, SupportsIndex, cast

from . import consolidation_authority, consolidation_plan

TRANSPORT_VERIFICATION_BASIS_SCHEMA = (
    "exomem.consolidation-transport-verification-basis/v1"
)
TRANSPORT_VERIFICATION_PROBE_SCHEMA = (
    "exomem.consolidation-transport-verification-probe/v1"
)
TRANSPORT_VERIFICATION_PLAN_SCHEMA = (
    "exomem.consolidation-transport-verification-plan/v1"
)
EXACT_DESTINATION_BINDING_SCHEMA = (
    "exomem.consolidation-exact-destination-binding/v1"
)

_BASIS_DOMAIN = TRANSPORT_VERIFICATION_BASIS_SCHEMA.encode("ascii")
_PROBE_DOMAIN = TRANSPORT_VERIFICATION_PROBE_SCHEMA.encode("ascii")
_PLAN_DOMAIN = TRANSPORT_VERIFICATION_PLAN_SCHEMA.encode("ascii")
_EXACT_DESTINATION_DOMAIN = EXACT_DESTINATION_BINDING_SCHEMA.encode("ascii")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_UUID4 = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z"
)
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}\Z")
_SURFACES = ("mcp", "rest", "hosted", "cli")
_POLARITIES = ("positive", "negative")
_MAX_PROBES = 1024
_MAX_SAFE_INTEGER = (1 << 53) - 1
_ROUTE_SEAL = object()
_EXACT_DESTINATION_SEAL = object()
_PLAN_SEAL = object()
_ACTIVE_ROUTE: ContextVar[TransportProbeRoute | None] = ContextVar(
    "exomem_consolidation_transport_probe_route",
    default=None,
)
_BASIS_INPUT_FIELDS = frozenset(
    {
        "schema",
        "vault_binding_digest",
        "run_id",
        "operation_id",
        "plan_digest",
        "verification_manifest_digest",
        "canonical_census_digest",
        "release_build_digest",
        "surface_profile",
        "surface_descriptor_digest",
        "configuration_digest",
        "trust_digest",
        "principal_mapping_digest",
        "routing_stop_digest",
        "transport_supervisor_readiness_digest",
        "hosted_profile_selection_record_digest",
        "hosted_profile_selection_verifier_generation",
        "hosted_owner_entitlement_verifier_readiness_digest",
    }
)
_CONTRACT_FIELDS = frozenset(
    {
        "probe_id",
        "probe_kind",
        "surface",
        "contract_digest",
        "expected_result_digest",
    }
)

__all__ = [
    "TRANSPORT_VERIFICATION_BASIS_SCHEMA",
    "TRANSPORT_VERIFICATION_PLAN_SCHEMA",
    "TRANSPORT_VERIFICATION_PROBE_SCHEMA",
    "EXACT_DESTINATION_BINDING_SCHEMA",
    "ConsolidationTransportVerificationUnavailable",
    "TransportProbe",
    "TransportProbeRoute",
    "TransportVerificationBasis",
    "TransportVerificationPlan",
    "build_transport_verification_plan",
    "issue_exact_destination_binding",
    "issue_transport_probe_route",
    "transport_probe_route_scope",
]


class ConsolidationTransportVerificationUnavailable(RuntimeError):
    """Content-free refusal for an invalid exact-cell transport gate."""

    code = "CONSOLIDATION_TRANSPORT_VERIFICATION_UNAVAILABLE"

    def __init__(self) -> None:
        super().__init__(self.code)


def _fail() -> NoReturn:
    raise ConsolidationTransportVerificationUnavailable from None


def _digest(value: object) -> str:
    if type(value) is not str or _DIGEST.fullmatch(value) is None:
        _fail()
    return value


def _uuid4(value: object) -> str:
    if type(value) is not str or _UUID4.fullmatch(value) is None:
        _fail()
    return value


def _identifier(value: object) -> str:
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        _fail()
    return value


def _integer(value: object) -> int:
    if type(value) is not int or not 0 <= value <= _MAX_SAFE_INTEGER:
        _fail()
    return value


def _mapping(value: object, fields: frozenset[str]) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or frozenset(value) != fields:
        _fail()
    return value


def _framed_digest(domain: bytes, value: object) -> str:
    try:
        encoded = consolidation_plan.canonical_closed_jcs(value)
    except consolidation_plan.ConsolidationPlanUnavailable:
        _fail()
    framed = len(domain).to_bytes(4, "big") + domain + len(encoded).to_bytes(8, "big") + encoded
    return hashlib.sha256(framed).hexdigest()


@dataclass(frozen=True, slots=True)
class TransportVerificationBasis:
    schema: str
    vault_binding_digest: str
    run_id: str
    operation_id: str
    journal_digest: str
    plan_digest: str
    verification_manifest_digest: str
    canonical_census_digest: str
    release_build_digest: str
    surface_profile: str
    surface_descriptor_digest: str
    configuration_digest: str
    trust_digest: str
    principal_mapping_digest: str
    routing_stop_digest: str
    transport_supervisor_readiness_digest: str
    hosted_profile_selection_record_digest: str
    hosted_profile_selection_verifier_generation: int
    hosted_owner_entitlement_verifier_readiness_digest: str
    exact_destination_binding_digest: str
    destination_kind: Literal["real"]
    digest: str


@dataclass(frozen=True, slots=True)
class TransportProbe:
    schema: str
    ordinal: int
    probe_id: str
    probe_kind: Literal["positive", "negative"]
    surface: Literal["mcp", "rest", "hosted", "cli"]
    contract_digest: str
    expected_result_digest: str
    probe_digest: str


class _PlanAdmission:
    __slots__ = ("__plan_digest", "__seal")
    __plan_digest: str
    __seal: object

    def __init__(self, plan_digest: str, seal: object) -> None:
        object.__setattr__(self, "_PlanAdmission__plan_digest", plan_digest)
        object.__setattr__(self, "_PlanAdmission__seal", seal)

    def __setattr__(self, _name: str, _value: object) -> NoReturn:
        raise TypeError("transport verification plan admission is immutable")

    def _matches(self, plan_digest: str) -> bool:
        return self.__seal is _PLAN_SEAL and self.__plan_digest == plan_digest

    def __reduce__(self) -> NoReturn:
        raise TypeError("transport verification plan admission is process-local")

    def __copy__(self) -> NoReturn:
        raise TypeError("transport verification plan admission is process-local")

    def __deepcopy__(self, _memo: object) -> NoReturn:
        raise TypeError("transport verification plan admission is process-local")


@dataclass(frozen=True, slots=True)
class TransportVerificationPlan:
    schema: str
    basis: TransportVerificationBasis
    probes: tuple[TransportProbe, ...]
    digest: str
    _admission: object = field(repr=False, compare=False)


def _basis_value(basis: TransportVerificationBasis) -> dict[str, object]:
    return {
        "schema": basis.schema,
        "vault_binding_digest": basis.vault_binding_digest,
        "run_id": basis.run_id,
        "operation_id": basis.operation_id,
        "journal_digest": basis.journal_digest,
        "plan_digest": basis.plan_digest,
        "verification_manifest_digest": basis.verification_manifest_digest,
        "canonical_census_digest": basis.canonical_census_digest,
        "release_build_digest": basis.release_build_digest,
        "surface_profile": basis.surface_profile,
        "surface_descriptor_digest": basis.surface_descriptor_digest,
        "configuration_digest": basis.configuration_digest,
        "trust_digest": basis.trust_digest,
        "principal_mapping_digest": basis.principal_mapping_digest,
        "routing_stop_digest": basis.routing_stop_digest,
        "transport_supervisor_readiness_digest": (
            basis.transport_supervisor_readiness_digest
        ),
        "hosted_profile_selection_record_digest": (
            basis.hosted_profile_selection_record_digest
        ),
        "hosted_profile_selection_verifier_generation": (
            basis.hosted_profile_selection_verifier_generation
        ),
        "hosted_owner_entitlement_verifier_readiness_digest": (
            basis.hosted_owner_entitlement_verifier_readiness_digest
        ),
        "exact_destination_binding_digest": basis.exact_destination_binding_digest,
        "destination_kind": basis.destination_kind,
    }


def _basis_input_value(basis: TransportVerificationBasis) -> dict[str, object]:
    return {
        key: value
        for key, value in _basis_value(basis).items()
        if key
        not in {
            "destination_kind",
            "exact_destination_binding_digest",
            "journal_digest",
        }
    }


def _basis_from_input(value: object) -> TransportVerificationBasis:
    row = _mapping(value, _BASIS_INPUT_FIELDS)
    if row["schema"] != TRANSPORT_VERIFICATION_BASIS_SCHEMA:
        _fail()
    return TransportVerificationBasis(
        schema=TRANSPORT_VERIFICATION_BASIS_SCHEMA,
        vault_binding_digest=_digest(row["vault_binding_digest"]),
        run_id=_uuid4(row["run_id"]),
        operation_id=_uuid4(row["operation_id"]),
        journal_digest="0" * 64,
        plan_digest=_digest(row["plan_digest"]),
        verification_manifest_digest=_digest(row["verification_manifest_digest"]),
        canonical_census_digest=_digest(row["canonical_census_digest"]),
        release_build_digest=_digest(row["release_build_digest"]),
        surface_profile=_identifier(row["surface_profile"]),
        surface_descriptor_digest=_digest(row["surface_descriptor_digest"]),
        configuration_digest=_digest(row["configuration_digest"]),
        trust_digest=_digest(row["trust_digest"]),
        principal_mapping_digest=_digest(row["principal_mapping_digest"]),
        routing_stop_digest=_digest(row["routing_stop_digest"]),
        transport_supervisor_readiness_digest=_digest(
            row["transport_supervisor_readiness_digest"]
        ),
        hosted_profile_selection_record_digest=_digest(
            row["hosted_profile_selection_record_digest"]
        ),
        hosted_profile_selection_verifier_generation=_integer(
            row["hosted_profile_selection_verifier_generation"]
        ),
        hosted_owner_entitlement_verifier_readiness_digest=_digest(
            row["hosted_owner_entitlement_verifier_readiness_digest"]
        ),
        exact_destination_binding_digest="0" * 64,
        destination_kind="real",
        digest="0" * 64,
    )


class ExactDestinationBinding:
    """Opaque control proof that basis facts came from the exact stopped cell."""

    __slots__ = (
        "__basis_fingerprint",
        "__binding_digest",
        "__journal_digest",
        "__seal",
    )
    __basis_fingerprint: str
    __binding_digest: str
    __journal_digest: str
    __seal: object

    def __init__(
        self,
        basis_fingerprint: str,
        binding_digest: str,
        journal_digest: str,
        seal: object,
    ) -> None:
        object.__setattr__(
            self,
            "_ExactDestinationBinding__basis_fingerprint",
            basis_fingerprint,
        )
        object.__setattr__(
            self,
            "_ExactDestinationBinding__binding_digest",
            binding_digest,
        )
        object.__setattr__(
            self,
            "_ExactDestinationBinding__journal_digest",
            journal_digest,
        )
        object.__setattr__(self, "_ExactDestinationBinding__seal", seal)

    def __setattr__(self, _name: str, _value: object) -> NoReturn:
        raise TypeError("exact destination binding is immutable")

    def __repr__(self) -> str:
        return "<ExactDestinationBinding process-local>"

    def __reduce__(self) -> NoReturn:
        raise TypeError("exact destination binding is process-local")

    def __reduce_ex__(self, _protocol: SupportsIndex) -> NoReturn:
        raise TypeError("exact destination binding is process-local")

    def __copy__(self) -> NoReturn:
        raise TypeError("exact destination binding is process-local")

    def __deepcopy__(self, _memo: object) -> NoReturn:
        raise TypeError("exact destination binding is process-local")

    def _matches(self, basis: TransportVerificationBasis) -> bool:
        return self.__seal is _EXACT_DESTINATION_SEAL and self.__basis_fingerprint == (
            _framed_digest(_BASIS_DOMAIN, _basis_input_value(basis))
        )

    def _binding_digest(self) -> str:
        if self.__seal is not _EXACT_DESTINATION_SEAL:
            _fail()
        return self.__binding_digest

    def _journal_digest(self) -> str:
        if self.__seal is not _EXACT_DESTINATION_SEAL:
            _fail()
        return self.__journal_digest


def issue_exact_destination_binding(
    authority: object,
    *,
    journal_digest: str,
    basis: Mapping[str, object],
) -> ExactDestinationBinding:
    """Bind trusted routing-stop observations to the exact operation and cell."""

    checked_basis = _basis_from_input(basis)
    checked_journal = _digest(journal_digest)
    try:
        consolidation_authority.require_authority(
            authority,
            vault_binding_digest=checked_basis.vault_binding_digest,
            run_id=checked_basis.run_id,
            operation_id=checked_basis.operation_id,
            journal_digest=checked_journal,
            phase="transport-stopping",
            action="apply",
        )
    except consolidation_authority.ConsolidationAuthorityUnavailable:
        _fail()
    basis_fingerprint = _framed_digest(
        _BASIS_DOMAIN,
        _basis_input_value(checked_basis),
    )
    binding_digest = _framed_digest(
        _EXACT_DESTINATION_DOMAIN,
        {
            "schema": EXACT_DESTINATION_BINDING_SCHEMA,
            "basis_fingerprint": basis_fingerprint,
            "journal_digest": checked_journal,
        },
    )
    return ExactDestinationBinding(
        basis_fingerprint,
        binding_digest,
        checked_journal,
        _EXACT_DESTINATION_SEAL,
    )


def _checked_basis(
    value: object,
    *,
    exact_destination_binding: object,
) -> TransportVerificationBasis:
    basis = _basis_from_input(value)
    if (
        type(exact_destination_binding) is not ExactDestinationBinding
        or not exact_destination_binding._matches(basis)
    ):
        _fail()
    exact_digest = exact_destination_binding._binding_digest()
    journal_digest = exact_destination_binding._journal_digest()
    basis = TransportVerificationBasis(
        **{
            **{
                field: getattr(basis, field)
                for field in TransportVerificationBasis.__slots__
                if field
                not in {
                    "digest",
                    "exact_destination_binding_digest",
                    "journal_digest",
                }
            },
            "exact_destination_binding_digest": exact_digest,
            "journal_digest": journal_digest,
            "digest": "0" * 64,
        }
    )
    return TransportVerificationBasis(
        **{
            **{
                field: getattr(basis, field)
                for field in TransportVerificationBasis.__slots__
                if field != "digest"
            },
            "digest": _framed_digest(_BASIS_DOMAIN, _basis_value(basis)),
        }
    )


def _probe_value(probe: TransportProbe) -> dict[str, object]:
    return {
        "schema": probe.schema,
        "ordinal": probe.ordinal,
        "probe_id": probe.probe_id,
        "probe_kind": probe.probe_kind,
        "surface": probe.surface,
        "contract_digest": probe.contract_digest,
        "expected_result_digest": probe.expected_result_digest,
    }


def _checked_probe(value: object, *, ordinal: int) -> TransportProbe:
    row = _mapping(value, _CONTRACT_FIELDS)
    surface = row["surface"]
    probe_kind = row["probe_kind"]
    if surface not in _SURFACES or probe_kind not in _POLARITIES:
        _fail()
    probe = TransportProbe(
        schema=TRANSPORT_VERIFICATION_PROBE_SCHEMA,
        ordinal=ordinal,
        probe_id=_identifier(row["probe_id"]),
        probe_kind=cast(Literal["positive", "negative"], probe_kind),
        surface=cast(Literal["mcp", "rest", "hosted", "cli"], surface),
        contract_digest=_digest(row["contract_digest"]),
        expected_result_digest=_digest(row["expected_result_digest"]),
        probe_digest="0" * 64,
    )
    return TransportProbe(
        schema=probe.schema,
        ordinal=probe.ordinal,
        probe_id=probe.probe_id,
        probe_kind=probe.probe_kind,
        surface=probe.surface,
        contract_digest=probe.contract_digest,
        expected_result_digest=probe.expected_result_digest,
        probe_digest=_framed_digest(_PROBE_DOMAIN, _probe_value(probe)),
    )


def build_transport_verification_plan(
    *,
    basis: Mapping[str, object],
    contracts: Sequence[Mapping[str, object]],
    exact_destination_binding: object,
) -> TransportVerificationPlan:
    """Build one real-cell plan with both polarities on every public surface."""

    if (
        isinstance(contracts, (str, bytes))
        or not isinstance(contracts, Sequence)
        or not 1 <= len(contracts) <= _MAX_PROBES
    ):
        _fail()
    checked_basis = _checked_basis(
        basis,
        exact_destination_binding=exact_destination_binding,
    )
    probes = tuple(
        _checked_probe(contract, ordinal=ordinal)
        for ordinal, contract in enumerate(contracts)
    )
    if len({probe.probe_id for probe in probes}) != len(probes):
        _fail()
    coverage = {(probe.surface, probe.probe_kind) for probe in probes}
    if coverage != {
        (surface, polarity)
        for surface in _SURFACES
        for polarity in _POLARITIES
    }:
        _fail()
    value = {
        "schema": TRANSPORT_VERIFICATION_PLAN_SCHEMA,
        "basis": {**_basis_value(checked_basis), "basis_digest": checked_basis.digest},
        "probes": tuple(
            {**_probe_value(probe), "probe_digest": probe.probe_digest}
            for probe in probes
        ),
    }
    plan_digest = _framed_digest(_PLAN_DOMAIN, value)
    return TransportVerificationPlan(
        schema=TRANSPORT_VERIFICATION_PLAN_SCHEMA,
        basis=checked_basis,
        probes=probes,
        digest=plan_digest,
        _admission=_PlanAdmission(plan_digest, _PLAN_SEAL),
    )


def _checked_plan_member(
    plan: object,
    probe: object,
) -> tuple[TransportVerificationPlan, TransportProbe]:
    if type(plan) is not TransportVerificationPlan or type(probe) is not TransportProbe:
        _fail()
    if type(plan._admission) is not _PlanAdmission or not plan._admission._matches(
        plan.digest
    ):
        _fail()
    basis = plan.basis
    if (
        type(basis) is not TransportVerificationBasis
        or plan.schema != TRANSPORT_VERIFICATION_PLAN_SCHEMA
        or basis.schema != TRANSPORT_VERIFICATION_BASIS_SCHEMA
        or basis.destination_kind != "real"
        or _uuid4(basis.run_id) != basis.run_id
        or _uuid4(basis.operation_id) != basis.operation_id
        or _identifier(basis.surface_profile) != basis.surface_profile
        or _integer(basis.hosted_profile_selection_verifier_generation)
        != basis.hosted_profile_selection_verifier_generation
    ):
        _fail()
    for digest in (
        basis.vault_binding_digest,
        basis.journal_digest,
        basis.plan_digest,
        basis.verification_manifest_digest,
        basis.canonical_census_digest,
        basis.release_build_digest,
        basis.surface_descriptor_digest,
        basis.configuration_digest,
        basis.trust_digest,
        basis.principal_mapping_digest,
        basis.routing_stop_digest,
        basis.transport_supervisor_readiness_digest,
        basis.hosted_profile_selection_record_digest,
        basis.hosted_owner_entitlement_verifier_readiness_digest,
        basis.exact_destination_binding_digest,
        basis.digest,
    ):
        _digest(digest)
    if basis.digest != _framed_digest(_BASIS_DOMAIN, _basis_value(basis)):
        _fail()
    if (
        type(plan.probes) is not tuple
        or type(probe.ordinal) is not int
        or not 0 <= probe.ordinal < len(plan.probes)
        or plan.probes[probe.ordinal] != probe
    ):
        _fail()
    for ordinal, candidate in enumerate(plan.probes):
        if (
            type(candidate) is not TransportProbe
            or candidate.schema != TRANSPORT_VERIFICATION_PROBE_SCHEMA
            or type(candidate.ordinal) is not int
            or candidate.ordinal != ordinal
            or candidate.surface not in _SURFACES
            or candidate.probe_kind not in _POLARITIES
            or _identifier(candidate.probe_id) != candidate.probe_id
            or _digest(candidate.contract_digest) != candidate.contract_digest
            or _digest(candidate.expected_result_digest)
            != candidate.expected_result_digest
            or _digest(candidate.probe_digest) != candidate.probe_digest
            or candidate.probe_digest
            != _framed_digest(_PROBE_DOMAIN, _probe_value(candidate))
        ):
            _fail()
    coverage = {(candidate.surface, candidate.probe_kind) for candidate in plan.probes}
    if coverage != {
        (surface, polarity)
        for surface in _SURFACES
        for polarity in _POLARITIES
    }:
        _fail()
    value = {
        "schema": TRANSPORT_VERIFICATION_PLAN_SCHEMA,
        "basis": {**_basis_value(basis), "basis_digest": basis.digest},
        "probes": tuple(
            {**_probe_value(candidate), "probe_digest": candidate.probe_digest}
            for candidate in plan.probes
        ),
    }
    if _digest(plan.digest) != _framed_digest(_PLAN_DOMAIN, value):
        _fail()
    return plan, probe


@dataclass(slots=True)
class _RouteLease:
    active: bool = False
    closed: bool = False


class TransportProbeRoute:
    """Opaque process-local permission for one supervised isolated probe route."""

    __slots__ = (
        "__journal_digest",
        "__lease",
        "__operation_id",
        "__plan_digest",
        "__probe_digest",
        "__run_id",
        "__seal",
        "__vault_binding_digest",
    )
    __vault_binding_digest: str
    __run_id: str
    __operation_id: str
    __journal_digest: str
    __plan_digest: str
    __probe_digest: str
    __seal: object
    __lease: _RouteLease

    def __init__(
        self,
        vault_binding_digest: str,
        run_id: str,
        operation_id: str,
        journal_digest: str,
        plan_digest: str,
        probe_digest: str,
        lease: _RouteLease,
        seal: object,
    ) -> None:
        object.__setattr__(self, "_TransportProbeRoute__vault_binding_digest", vault_binding_digest)
        object.__setattr__(self, "_TransportProbeRoute__run_id", run_id)
        object.__setattr__(self, "_TransportProbeRoute__operation_id", operation_id)
        object.__setattr__(self, "_TransportProbeRoute__journal_digest", journal_digest)
        object.__setattr__(self, "_TransportProbeRoute__plan_digest", plan_digest)
        object.__setattr__(self, "_TransportProbeRoute__probe_digest", probe_digest)
        object.__setattr__(self, "_TransportProbeRoute__lease", lease)
        object.__setattr__(self, "_TransportProbeRoute__seal", seal)

    def __setattr__(self, _name: str, _value: object) -> NoReturn:
        raise TypeError("transport probe route is immutable")

    def __repr__(self) -> str:
        return "<TransportProbeRoute process-local>"

    def __reduce__(self) -> NoReturn:
        raise TypeError("transport probe route is process-local")

    def __reduce_ex__(self, _protocol: SupportsIndex) -> NoReturn:
        raise TypeError("transport probe route is process-local")

    def __copy__(self) -> NoReturn:
        raise TypeError("transport probe route is process-local")

    def __deepcopy__(self, _memo: object) -> NoReturn:
        raise TypeError("transport probe route is process-local")

    def _matches(
        self,
        *,
        vault_binding_digest: str,
        run_id: str,
        operation_id: str,
        journal_digest: str,
    ) -> bool:
        return self.__seal is _ROUTE_SEAL and self.__lease.active and (
            self.__vault_binding_digest,
            self.__run_id,
            self.__operation_id,
            self.__journal_digest,
        ) == (vault_binding_digest, run_id, operation_id, journal_digest)

    def _matches_plan_probe(
        self,
        *,
        plan_digest: str,
        probe_digest: str,
    ) -> bool:
        return self.__seal is _ROUTE_SEAL and (
            self.__plan_digest,
            self.__probe_digest,
        ) == (plan_digest, probe_digest)

    def _begin(self, *, plan_digest: str, probe_digest: str) -> None:
        if (
            self.__lease.active
            or self.__lease.closed
            or not self._matches_plan_probe(
                plan_digest=plan_digest,
                probe_digest=probe_digest,
            )
        ):
            _fail()
        self.__lease.active = True

    def _finish(self) -> None:
        self.__lease.active = False
        self.__lease.closed = True


def issue_transport_probe_route(
    authority: object,
    *,
    plan: object,
    probe: object,
) -> TransportProbeRoute:
    """Consume exact probe authority before entering an auth-transparent route."""

    checked_plan, checked_probe = _checked_plan_member(plan, probe)
    checked_vault = checked_plan.basis.vault_binding_digest
    checked_run = checked_plan.basis.run_id
    checked_operation = checked_plan.basis.operation_id
    checked_journal = checked_plan.basis.journal_digest
    try:
        consolidation_authority.require_authority(
            authority,
            vault_binding_digest=checked_vault,
            run_id=checked_run,
            operation_id=checked_operation,
            journal_digest=checked_journal,
            phase="transport-verifying",
            action="probe",
        )
    except consolidation_authority.ConsolidationAuthorityUnavailable:
        _fail()
    return TransportProbeRoute(
        checked_vault,
        checked_run,
        checked_operation,
        checked_journal,
        checked_plan.digest,
        checked_probe.probe_digest,
        _RouteLease(),
        _ROUTE_SEAL,
    )


@contextmanager
def transport_probe_route_scope(
    route: object,
    *,
    plan: object,
    probe: object,
) -> Iterator[None]:
    """Mark only the supervisor's current execution context as the isolated route."""

    checked_plan, checked_probe = _checked_plan_member(plan, probe)
    if type(route) is not TransportProbeRoute or _ACTIVE_ROUTE.get() is not None:
        _fail()
    route._begin(
        plan_digest=checked_plan.digest,
        probe_digest=checked_probe.probe_digest,
    )
    token = _ACTIVE_ROUTE.set(route)
    try:
        yield
    finally:
        route._finish()
        _ACTIVE_ROUTE.reset(token)


def _active_route_matches(
    *,
    vault_binding_digest: str,
    run_id: str | None,
    operation_id: str | None,
    journal_digest: str | None,
    phase: str | None,
) -> bool:
    """Internal admission seam; route state never comes from request data."""

    route = _ACTIVE_ROUTE.get()
    return (
        type(route) is TransportProbeRoute
        and phase == "transport-verifying"
        and type(run_id) is str
        and type(operation_id) is str
        and type(journal_digest) is str
        and route._matches(
            vault_binding_digest=vault_binding_digest,
            run_id=run_id,
            operation_id=operation_id,
            journal_digest=journal_digest,
        )
    )
