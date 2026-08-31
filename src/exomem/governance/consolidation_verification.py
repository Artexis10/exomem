"""Closed, plan-bound in-process probes for governed consolidation.

Probe definitions are owner-protected control data.  Receipts see only their
digests and bounded outcome class; probe refs never cross the evidence boundary.
The process-local consolidation authority is carried only in memory.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, NoReturn

from . import consolidation_plan

VERIFICATION_PROBE_SCHEMA = "exomem.consolidation-verification-probe/v1"
VERIFICATION_PROBE_TERMINAL_SCHEMA = "exomem.consolidation-verification-probe-terminal/v1"
POSITIVE_PROBE_SET_SCHEMA = "exomem.consolidation-positive-probes/v1"
NEGATIVE_PROBE_SET_SCHEMA = "exomem.consolidation-negative-probes/v1"
CANONICAL_SURFACE_EXECUTOR_ID = "canonical-governance-surface-v1"

_PROBE_DOMAIN = VERIFICATION_PROBE_SCHEMA.encode("ascii")
_POSITIVE_DOMAIN = POSITIVE_PROBE_SET_SCHEMA.encode("ascii")
_NEGATIVE_DOMAIN = NEGATIVE_PROBE_SET_SCHEMA.encode("ascii")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_IDENTIFIER = re.compile(r"[a-z][a-z0-9-]{0,127}\Z")
_PROBE_FIELDS = frozenset({"probe_id", "executor_id", "contract_digest", "expected_result_digest"})
_MAX_PROBES = 1024

__all__ = [
    "NEGATIVE_PROBE_SET_SCHEMA",
    "POSITIVE_PROBE_SET_SCHEMA",
    "CANONICAL_SURFACE_EXECUTOR_ID",
    "VERIFICATION_PROBE_SCHEMA",
    "VERIFICATION_PROBE_TERMINAL_SCHEMA",
    "ConsolidationVerificationUnavailable",
    "VerificationPlan",
    "VerificationProbe",
    "VerificationProbeContext",
    "VerificationProbeTerminal",
    "build_verification_plan",
]


class ConsolidationVerificationUnavailable(RuntimeError):
    """Content-free refusal for an invalid plan, probe, or result."""

    code = "CONSOLIDATION_VERIFICATION_UNAVAILABLE"

    def __init__(self) -> None:
        super().__init__(self.code)


@dataclass(frozen=True, slots=True)
class VerificationProbe:
    schema: str
    ordinal: int
    probe_kind: Literal["positive", "negative"]
    probe_id: str
    executor_id: str
    contract_digest: str
    expected_result_digest: str
    probe_digest: str


@dataclass(frozen=True, slots=True)
class VerificationPlan:
    positive_probes: tuple[VerificationProbe, ...]
    negative_probes: tuple[VerificationProbe, ...]
    positive_probe_digest: str
    negative_probe_digest: str

    @property
    def probes(self) -> tuple[VerificationProbe, ...]:
        return self.positive_probes + self.negative_probes


@dataclass(frozen=True, slots=True)
class VerificationProbeContext:
    vault_root: Path
    canonical_census_digest: str
    verification_basis_digest: str
    authority: object


@dataclass(frozen=True, slots=True)
class VerificationProbeTerminal:
    schema: str
    probe_id: str
    probe_digest: str
    result_digest: str
    outcome: Literal["passed"]


def _fail() -> NoReturn:
    raise ConsolidationVerificationUnavailable from None


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


def _probe_value(probe: VerificationProbe) -> dict[str, object]:
    return {
        "schema": probe.schema,
        "ordinal": probe.ordinal,
        "probe_kind": probe.probe_kind,
        "probe_id": probe.probe_id,
        "executor_id": probe.executor_id,
        "contract_digest": probe.contract_digest,
        "expected_result_digest": probe.expected_result_digest,
    }


def _build_probe(
    value: object,
    *,
    ordinal: int,
    probe_kind: Literal["positive", "negative"],
) -> VerificationProbe:
    if not isinstance(value, Mapping) or frozenset(value) != _PROBE_FIELDS:
        _fail()
    candidate = VerificationProbe(
        schema=VERIFICATION_PROBE_SCHEMA,
        ordinal=ordinal,
        probe_kind=probe_kind,
        probe_id=_identifier(value["probe_id"]),
        executor_id=_identifier(value["executor_id"]),
        contract_digest=_digest(value["contract_digest"]),
        expected_result_digest=_digest(value["expected_result_digest"]),
        probe_digest="0" * 64,
    )
    if candidate.executor_id != CANONICAL_SURFACE_EXECUTOR_ID:
        _fail()
    return VerificationProbe(
        schema=candidate.schema,
        ordinal=candidate.ordinal,
        probe_kind=candidate.probe_kind,
        probe_id=candidate.probe_id,
        executor_id=candidate.executor_id,
        contract_digest=candidate.contract_digest,
        expected_result_digest=candidate.expected_result_digest,
        probe_digest=_framed_digest(_PROBE_DOMAIN, _probe_value(candidate)),
    )


def _sequence(value: object) -> Sequence[object]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        _fail()
    if not 1 <= len(value) <= _MAX_PROBES:
        _fail()
    return value


def _set_digest(
    probes: tuple[VerificationProbe, ...],
    *,
    schema: str,
    domain: bytes,
) -> str:
    return _framed_digest(
        domain,
        {
            "schema": schema,
            "probes": tuple(
                {**_probe_value(probe), "probe_digest": probe.probe_digest} for probe in probes
            ),
        },
    )


def build_verification_plan(
    *,
    positive_probes: Sequence[Mapping[str, object]],
    negative_probes: Sequence[Mapping[str, object]],
) -> VerificationPlan:
    """Build the exact positive/negative matrices bound by a cutover plan."""

    positive_values = _sequence(positive_probes)
    negative_values = _sequence(negative_probes)
    if len(positive_values) + len(negative_values) > _MAX_PROBES:
        _fail()
    positive = tuple(
        _build_probe(value, ordinal=ordinal, probe_kind="positive")
        for ordinal, value in enumerate(positive_values)
    )
    negative = tuple(
        _build_probe(
            value,
            ordinal=len(positive) + ordinal,
            probe_kind="negative",
        )
        for ordinal, value in enumerate(negative_values)
    )
    ids = tuple(probe.probe_id for probe in positive + negative)
    if len(set(ids)) != len(ids):
        _fail()
    return VerificationPlan(
        positive_probes=positive,
        negative_probes=negative,
        positive_probe_digest=_set_digest(
            positive,
            schema=POSITIVE_PROBE_SET_SCHEMA,
            domain=_POSITIVE_DOMAIN,
        ),
        negative_probe_digest=_set_digest(
            negative,
            schema=NEGATIVE_PROBE_SET_SCHEMA,
            domain=_NEGATIVE_DOMAIN,
        ),
    )


def _checked_plan(value: object) -> VerificationPlan:
    if type(value) is not VerificationPlan:
        _fail()
    rebuilt = build_verification_plan(
        positive_probes=tuple(
            {
                "probe_id": probe.probe_id,
                "executor_id": probe.executor_id,
                "contract_digest": probe.contract_digest,
                "expected_result_digest": probe.expected_result_digest,
            }
            for probe in value.positive_probes
        ),
        negative_probes=tuple(
            {
                "probe_id": probe.probe_id,
                "executor_id": probe.executor_id,
                "contract_digest": probe.contract_digest,
                "expected_result_digest": probe.expected_result_digest,
            }
            for probe in value.negative_probes
        ),
    )
    if rebuilt != value:
        _fail()
    return rebuilt


def _terminal(
    value: object,
    *,
    probe: VerificationProbe,
) -> VerificationProbeTerminal:
    if type(value) is not VerificationProbeTerminal:
        _fail()
    if (
        value.schema != VERIFICATION_PROBE_TERMINAL_SCHEMA
        or value.probe_id != probe.probe_id
        or value.probe_digest != probe.probe_digest
        or value.result_digest != probe.expected_result_digest
        or value.outcome != "passed"
    ):
        _fail()
    return VerificationProbeTerminal(
        schema=value.schema,
        probe_id=value.probe_id,
        probe_digest=_digest(value.probe_digest),
        result_digest=_digest(value.result_digest),
        outcome="passed",
    )


def _run_probe(
    runner: Callable[[VerificationProbe, VerificationProbeContext], VerificationProbeTerminal],
    probe: VerificationProbe,
    context: VerificationProbeContext,
) -> VerificationProbeTerminal:
    try:
        return _terminal(runner(probe, context), probe=probe)
    except ConsolidationVerificationUnavailable:
        raise
    except Exception:  # noqa: BLE001 - never disclose probe failure details
        _fail()
