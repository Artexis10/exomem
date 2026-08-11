"""Scenario trajectory loader.

A scenario is a phase-keyed state trajectory: ordered operations drawn from a
fixed vocabulary, and per-phase expectations that name assertions. The names
are resolved against the frozen registry **at load time**, so an unknown
assertion is a hard error before any provider runs rather than a surprise in
the middle of a scored run. The same is true of the fairness packet: a scenario
without one refuses to load, because a scenario nobody has argued is
product-neutral has no business producing a comparative number.

Strictness note: the YAML is re-serialized to JSON and validated in JSON mode.
Pydantic strict mode rejects a Python ``list`` for a ``tuple`` field, which is
exactly what ``yaml.safe_load`` produces; going through JSON keeps the models
strict and immutable without loosening validation.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Literal

import yaml
from pydantic import Field, ValidationError, model_validator

from .assertions import AssertionContext, AssertionResult
from .catastrophic import CATASTROPHIC_ASSERTIONS
from .registry import (
    ASSERTION_REGISTRY,
    PREREGISTERED_FAMILY_IDS,
    REQUIRES_ITEM_PAIR,
    REQUIRES_SNAPSHOT_PAIR,
    RegistryError,
    resolve,
)
from .snapshot import StrictModel

#: The operation vocabulary. ``corpus`` scenarios use the first two; the
#: out-of-band operations are what make an ``operational`` family a test of the
#: engine rather than of the retriever.
OpKind = Literal[
    "ingest_source",
    "agent_turn",
    "external_edit",
    "stop_engine",
    "start_engine",
    "fresh_agent",
    "export",
    "snapshot",
]

ScenarioKind = Literal["corpus", "operational"]


class ScenarioLoadError(ValueError):
    """Any failure that must stop the suite before a provider runs."""


class ScenarioOp(StrictModel):
    """One trajectory operation. ``ref`` names the corpus artifact or snapshot."""

    op: OpKind
    ref: str = Field(min_length=1)
    at: str | None = None
    detail: str = ""


class Expectation(StrictModel):
    """One named assertion plus the parameters it needs."""

    assertion: str = Field(min_length=1, alias="assert")
    subject: str | None = None
    counterpart: str | None = None
    tolerance: float | None = None
    freshness_bound_s: float | None = None


class FairnessMechanism(StrictModel):
    """How one covered provider could satisfy the invariant, with a verdict.

    ``provider_role`` is deliberately a role, not a product name: the engine
    stays product-neutral and the concrete competitor mapping lives in the
    per-competitor packets that later lanes add.
    """

    provider_role: str = Field(min_length=1)
    mechanism: str = Field(min_length=1)
    verdict: str = Field(min_length=1)
    evidence: str = Field(min_length=1)


class PrivilegedEndpointMatrixEntry(StrictModel):
    """One exact driver-surface × provider × variant audit disposition."""

    driver_surface_id: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    variant: str = Field(min_length=1)
    disposition: Literal["equivalent", "capability_gap"]
    audit_scope: str = Field(min_length=1)
    evidence: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    competitor_surface: str | None = None

    @model_validator(mode="after")
    def _closed_disposition(self) -> "PrivilegedEndpointMatrixEntry":
        if self.disposition == "equivalent" and not self.competitor_surface:
            raise ValueError("equivalent disposition requires a competitor surface")
        if self.disposition == "capability_gap" and self.competitor_surface is not None:
            raise ValueError("capability_gap cannot carry a competitor surface")
        return self


class FairnessPacket(StrictModel):
    """Required. A scenario without a complete packet does not run."""

    why_neutral: str = Field(min_length=1)
    public_coverage_subtraction: str = Field(min_length=1)
    mechanisms: tuple[FairnessMechanism, ...] = Field(min_length=1)
    privileged_endpoint_matrix: tuple[PrivilegedEndpointMatrixEntry, ...] = Field(min_length=1)
    acceptance_predicate: str = Field(min_length=1)

    @model_validator(mode="after")
    def _matrix_keys_are_unique(self) -> "FairnessPacket":
        keys = [
            (row.driver_surface_id, row.provider, row.variant)
            for row in self.privileged_endpoint_matrix
        ]
        if len(keys) != len(set(keys)):
            raise ValueError("privileged endpoint matrix keys must be unique")
        return self


class ScenarioPhase(StrictModel):
    phase_id: str = Field(min_length=1)
    ops: tuple[ScenarioOp, ...] = Field(min_length=1)
    expect: tuple[Expectation, ...] = ()
    catastrophic_if_failed: tuple[str, ...] = ()


class Scenario(StrictModel):
    scenario_id: str = Field(min_length=1)
    family_id: str = Field(min_length=1)
    kind: ScenarioKind
    public_coverage: str = Field(min_length=1)
    phases: tuple[ScenarioPhase, ...] = Field(min_length=1)
    fairness: FairnessPacket

    def expectations(self) -> tuple[tuple[str, Expectation], ...]:
        """(phase_id, expectation) pairs in trajectory order."""

        return tuple(
            (phase.phase_id, expectation)
            for phase in self.phases
            for expectation in phase.expect
        )

    def bound_assertions(
        self,
    ) -> tuple[tuple[str, Callable[[AssertionContext], AssertionResult]], ...]:
        """Resolve every expectation to its callable. Raises if any is unknown."""

        return tuple(
            (expectation.assertion, resolve(expectation.assertion))
            for _phase_id, expectation in self.expectations()
        )

def _validate_names(scenario: Scenario, source: str) -> None:
    if scenario.family_id not in PREREGISTERED_FAMILY_IDS:
        raise ScenarioLoadError(
            f"{source}: family_id {scenario.family_id!r} is not in the pre-registered "
            "§1 family table"
        )

    unknown = [
        expectation.assertion
        for _phase_id, expectation in scenario.expectations()
        if expectation.assertion not in ASSERTION_REGISTRY
    ]
    if unknown:
        raise ScenarioLoadError(
            f"{source}: unknown assertion name(s) {sorted(set(unknown))!r}; "
            "the registry is frozen by the pre-registration"
        )

    # The §3 catastrophic set is frozen with the pre-registration hash. A
    # scenario may restate a member of it, but it may not promote anything else
    # into an integrity failure — that would let a scenario author change what
    # suppresses a provider's aggregates after the fact.
    stray = sorted(
        {
            name
            for phase in scenario.phases
            for name in phase.catastrophic_if_failed
            if name not in CATASTROPHIC_ASSERTIONS
        }
    )
    if stray:
        raise ScenarioLoadError(
            f"{source}: catastrophic_if_failed names {stray!r}, which are not in the "
            "frozen PREREGISTRATION §3 catastrophic set; a scenario cannot widen it"
        )


def _validate_pair_requirements(scenario: Scenario, source: str) -> None:
    """Assertions that compare two things must be given two things."""

    snapshots_so_far = 0
    for phase in scenario.phases:
        snapshots_so_far += sum(1 for op in phase.ops if op.op == "snapshot")
        for expectation in phase.expect:
            assertion = expectation.assertion
            if assertion in REQUIRES_ITEM_PAIR and not (
                expectation.subject and expectation.counterpart
            ):
                raise ScenarioLoadError(
                    f"{source}: phase {phase.phase_id!r} expects {assertion!r}, which "
                    "compares two named items, but the expectation does not declare "
                    "both a subject and a counterpart"
                )
            if assertion in REQUIRES_SNAPSHOT_PAIR and snapshots_so_far < 2:
                raise ScenarioLoadError(
                    f"{source}: phase {phase.phase_id!r} expects {assertion!r}, which is "
                    f"evaluated over a snapshot pair, but the trajectory has taken "
                    f"{snapshots_so_far} snapshot op(s) by this point"
                )
            if (
                assertion == "external_edit_authoritative_within"
                and expectation.freshness_bound_s is None
            ):
                raise ScenarioLoadError(
                    f"{source}: phase {phase.phase_id!r} expects {assertion!r} without a "
                    "declared freshness_bound_s; adoption latency would be unscoreable"
                )


def load_scenario_text(text: str, *, source: str) -> Scenario:
    """Parse and fully validate one scenario document."""

    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as error:
        raise ScenarioLoadError(f"{source}: not valid YAML: {error}") from error
    if not isinstance(data, dict):
        raise ScenarioLoadError(f"{source}: scenario must be a mapping")
    try:
        payload = json.dumps(data, default=str, sort_keys=True)
    except (TypeError, ValueError) as error:
        raise ScenarioLoadError(f"{source}: scenario is not JSON-serializable: {error}") from error
    try:
        scenario = Scenario.model_validate_json(payload)
    except ValidationError as error:
        raise ScenarioLoadError(f"{source}: {error}") from error
    try:
        _validate_names(scenario, source)
        _validate_pair_requirements(scenario, source)
        scenario.bound_assertions()
    except RegistryError as error:
        raise ScenarioLoadError(f"{source}: {error}") from error
    return scenario


def load_scenario(path: Path | str) -> Scenario:
    """Load one ``scenario.yaml``; every failure is a :class:`ScenarioLoadError`."""

    file_path = Path(path)
    try:
        text = file_path.read_text(encoding="utf-8")
    except OSError as error:
        raise ScenarioLoadError(f"{file_path.name}: cannot read scenario: {error}") from error
    return load_scenario_text(text, source=file_path.name)


def load_scenarios(directory: Path | str) -> tuple[Scenario, ...]:
    """Load every ``*.yaml`` in ``directory``, sorted by filename."""

    root = Path(directory)
    return tuple(load_scenario(path) for path in sorted(root.glob("*.yaml")))
