"""Exact-cell transport verification basis and process-local probe route."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
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

_BASIS_DOMAIN = TRANSPORT_VERIFICATION_BASIS_SCHEMA.encode("ascii")
_PROBE_DOMAIN = TRANSPORT_VERIFICATION_PROBE_SCHEMA.encode("ascii")
_PLAN_DOMAIN = TRANSPORT_VERIFICATION_PLAN_SCHEMA.encode("ascii")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_UUID4 = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z"
)
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}\Z")
_SURFACES = ("mcp", "rest", "hosted", "cli")
_POLARITIES = ("positive", "negative")
_MAX_PROBES = 1024
_ROUTE_SEAL = object()
_ACTIVE_ROUTE: ContextVar[TransportProbeRoute | None] = ContextVar(
    "exomem_consolidation_transport_probe_route",
    default=None,
)
_BASIS_FIELDS = frozenset(
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
        "destination_kind",
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
    "ConsolidationTransportVerificationUnavailable",
    "TransportProbe",
    "TransportProbeRoute",
    "TransportVerificationBasis",
    "TransportVerificationPlan",
    "build_transport_verification_plan",
    "issue_transport_probe_route",
    "require_active_transport_probe_route",
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


@dataclass(frozen=True, slots=True)
class TransportVerificationPlan:
    schema: str
    basis: TransportVerificationBasis
    probes: tuple[TransportProbe, ...]
    digest: str


def _basis_value(basis: TransportVerificationBasis) -> dict[str, object]:
    return {
        "schema": basis.schema,
        "vault_binding_digest": basis.vault_binding_digest,
        "run_id": basis.run_id,
        "operation_id": basis.operation_id,
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
        "destination_kind": basis.destination_kind,
    }


def _checked_basis(value: object) -> TransportVerificationBasis:
    row = _mapping(value, _BASIS_FIELDS)
    if (
        row["schema"] != TRANSPORT_VERIFICATION_BASIS_SCHEMA
        or row["destination_kind"] != "real"
    ):
        _fail()
    basis = TransportVerificationBasis(
        schema=TRANSPORT_VERIFICATION_BASIS_SCHEMA,
        vault_binding_digest=_digest(row["vault_binding_digest"]),
        run_id=_uuid4(row["run_id"]),
        operation_id=_uuid4(row["operation_id"]),
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
        destination_kind="real",
        digest="0" * 64,
    )
    return TransportVerificationBasis(
        **{
            **{field: getattr(basis, field) for field in _BASIS_FIELDS},
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
) -> TransportVerificationPlan:
    """Build one real-cell plan with both polarities on every public surface."""

    if (
        isinstance(contracts, (str, bytes))
        or not isinstance(contracts, Sequence)
        or not 1 <= len(contracts) <= _MAX_PROBES
    ):
        _fail()
    checked_basis = _checked_basis(basis)
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
    return TransportVerificationPlan(
        schema=TRANSPORT_VERIFICATION_PLAN_SCHEMA,
        basis=checked_basis,
        probes=probes,
        digest=_framed_digest(_PLAN_DOMAIN, value),
    )


class TransportProbeRoute:
    """Opaque process-local permission for one supervised isolated probe route."""

    __slots__ = (
        "__journal_digest",
        "__operation_id",
        "__probe_digest",
        "__run_id",
        "__seal",
        "__vault_binding_digest",
    )
    __vault_binding_digest: str
    __run_id: str
    __operation_id: str
    __journal_digest: str
    __probe_digest: str
    __seal: object

    def __init__(
        self,
        vault_binding_digest: str,
        run_id: str,
        operation_id: str,
        journal_digest: str,
        probe_digest: str,
        seal: object,
    ) -> None:
        object.__setattr__(self, "_TransportProbeRoute__vault_binding_digest", vault_binding_digest)
        object.__setattr__(self, "_TransportProbeRoute__run_id", run_id)
        object.__setattr__(self, "_TransportProbeRoute__operation_id", operation_id)
        object.__setattr__(self, "_TransportProbeRoute__journal_digest", journal_digest)
        object.__setattr__(self, "_TransportProbeRoute__probe_digest", probe_digest)
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
        return self.__seal is _ROUTE_SEAL and (
            self.__vault_binding_digest,
            self.__run_id,
            self.__operation_id,
            self.__journal_digest,
        ) == (vault_binding_digest, run_id, operation_id, journal_digest)

    def _matches_probe(self, probe_digest: str) -> bool:
        return self.__seal is _ROUTE_SEAL and self.__probe_digest == probe_digest


def issue_transport_probe_route(
    authority: object,
    *,
    vault_binding_digest: str,
    run_id: str,
    operation_id: str,
    journal_digest: str,
    probe_digest: str,
) -> TransportProbeRoute:
    """Consume exact probe authority before entering an auth-transparent route."""

    checked_vault = _digest(vault_binding_digest)
    checked_run = _uuid4(run_id)
    checked_operation = _uuid4(operation_id)
    checked_journal = _digest(journal_digest)
    checked_probe = _digest(probe_digest)
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
        checked_probe,
        _ROUTE_SEAL,
    )


@contextmanager
def transport_probe_route_scope(route: object) -> Iterator[None]:
    """Mark only the supervisor's current execution context as the isolated route."""

    if type(route) is not TransportProbeRoute or _ACTIVE_ROUTE.get() is not None:
        _fail()
    token = _ACTIVE_ROUTE.set(route)
    try:
        yield
    finally:
        _ACTIVE_ROUTE.reset(token)


def require_active_transport_probe_route(*, probe_digest: str) -> None:
    """Bind the supervisor's exact precommitted probe before transport execution."""

    route = _ACTIVE_ROUTE.get()
    if (
        type(route) is not TransportProbeRoute
        or not route._matches_probe(_digest(probe_digest))
    ):
        _fail()


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
