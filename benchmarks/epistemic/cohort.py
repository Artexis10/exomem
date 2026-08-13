"""Exact cohort validation and control-signal disposition."""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from .assertions import AssertionResult
from .evidence import (
    AssertionEvidenceRef,
    EvidenceReplayError,
    _replay_assertion_evidence_payload,
)
from .snapshot import StrictModel


_SHA256 = r"^[0-9a-f]{64}$"
CONTROL_PROVIDERS = ("grep-markdown", "no-memory")


class CohortValidationError(ValueError):
    """Rows do not form the exact preregistered comparison cohort."""


class CohortExpectationIdentity(StrictModel):
    scenario_id: str = Field(min_length=1)
    scenario_sha256: str = Field(pattern=_SHA256)
    phase_id: str = Field(min_length=1)
    expectation_ordinal: int = Field(ge=1)
    assertion: str = Field(min_length=1)
    subject: str | None = None
    counterpart: str | None = None
    tolerance: float = 0.0
    freshness_bound_s: float | None = None


class CohortAssertionResult(StrictModel):
    identity: CohortExpectationIdentity
    result: AssertionResult
    evidence_ref: AssertionEvidenceRef | None = None
    signal_disposition: Literal["product_signal", "no_product_signal", "control"] = (
        "product_signal"
    )

    @model_validator(mode="after")
    def _result_has_reference(self) -> "CohortAssertionResult":
        if self.evidence_ref is None:
            raise ValueError("every cohort assertion requires an evidence reference")
        if self.result.name != self.identity.assertion:
            raise ValueError("assertion result name differs from cohort identity")
        return self


class EpistemicCohortRow(StrictModel):
    provider: str = Field(min_length=1)
    variant: str = Field(min_length=1)
    assertions: tuple[CohortAssertionResult, ...]


class ValidatedEpistemicCohort(StrictModel):
    artifact_type: Literal["validated-epistemic-cohort.v1"]
    schema_version: Literal[1]
    run_id: str = Field(min_length=1)
    rows: tuple[EpistemicCohortRow, ...]


def _identity_sequence(row: EpistemicCohortRow) -> tuple[CohortExpectationIdentity, ...]:
    return tuple(cell.identity for cell in row.assertions)


def _validate_shape(rows: tuple[EpistemicCohortRow, ...]) -> None:
    """Validate exact cohort membership/order without trusting result claims."""

    if not rows:
        raise CohortValidationError("validated cohort requires rows")
    names = tuple(row.provider for row in rows)
    for control in CONTROL_PROVIDERS:
        if names.count(control) != 1:
            raise CohortValidationError(f"validated cohort requires exactly one {control} control")
    baseline = _identity_sequence(rows[0])
    if not baseline:
        raise CohortValidationError("validated cohort requires a non-empty ordered cohort")
    for row in rows[1:]:
        if _identity_sequence(row) != baseline:
            raise CohortValidationError(f"{row.provider} differs from the exact ordered cohort")


def _replay_cell(
    *, run_root: Path, row: EpistemicCohortRow, cell: CohortAssertionResult
) -> None:
    if cell.evidence_ref is None:
        raise CohortValidationError(
            f"{row.provider}/{row.variant} cohort cell has no evidence reference"
        )
    try:
        payload, result = _replay_assertion_evidence_payload(run_root, cell.evidence_ref)
    except EvidenceReplayError as exc:
        raise CohortValidationError(
            f"{row.provider}/{row.variant} cohort evidence replay failed: {exc}"
        ) from exc
    expected_identity = (
        row.provider,
        row.variant,
        cell.identity.scenario_id,
        cell.identity.scenario_sha256,
        cell.identity.phase_id,
        cell.identity.expectation_ordinal,
        cell.identity.assertion,
        cell.identity.subject,
        cell.identity.counterpart,
        cell.identity.tolerance,
        cell.identity.freshness_bound_s,
    )
    evidence_identity = (
        payload.provider,
        payload.variant,
        payload.scenario_id,
        payload.scenario_sha256,
        payload.phase_id,
        payload.expectation_ordinal,
        payload.assertion,
        payload.parameters.subject,
        payload.parameters.counterpart,
        payload.parameters.tolerance,
        payload.parameters.freshness_bound_s,
    )
    if evidence_identity != expected_identity:
        raise CohortValidationError(
            f"{row.provider}/{row.variant} cohort evidence identity differs from its cell"
        )
    if result != cell.result:
        raise CohortValidationError(
            f"{row.provider}/{row.variant} cohort evidence result differs from its cell"
        )


def validate_epistemic_cohort(
    *, run_id: str, rows: tuple[EpistemicCohortRow, ...], run_root: Path | str
) -> ValidatedEpistemicCohort:
    _validate_shape(rows)
    baseline = _identity_sequence(rows[0])
    root = Path(run_root)
    for row in rows:
        for cell in row.assertions:
            _replay_cell(run_root=root, row=row, cell=cell)

    for control in CONTROL_PROVIDERS:
        row = next(candidate for candidate in rows if candidate.provider == control)
        scenario_ids = {identity.scenario_id for identity in baseline}
        for scenario_id in scenario_ids:
            cells = tuple(
                cell for cell in row.assertions if cell.identity.scenario_id == scenario_id
            )
            if not any(cell.result.outcome in {"pass", "fail"} for cell in cells):
                raise CohortValidationError(
                    f"{control} scenario {scenario_id} requires at least one pass or fail"
                )

    control_passes = {
        tuple(cell.identity.model_dump(mode="json").items())
        for row in rows
        if row.provider in CONTROL_PROVIDERS
        for cell in row.assertions
        if cell.result.outcome == "pass"
    }
    normalized_rows: list[EpistemicCohortRow] = []
    for row in rows:
        is_control = row.provider in CONTROL_PROVIDERS
        normalized = tuple(
            cell.model_copy(
                update={
                    "signal_disposition": (
                        "control"
                        if is_control
                        else "no_product_signal"
                        if tuple(cell.identity.model_dump(mode="json").items()) in control_passes
                        else "product_signal"
                    )
                }
            )
            for cell in row.assertions
        )
        normalized_rows.append(row.model_copy(update={"assertions": normalized}))
    return ValidatedEpistemicCohort(
        artifact_type="validated-epistemic-cohort.v1",
        schema_version=1,
        run_id=run_id,
        rows=tuple(normalized_rows),
    )


def iter_strategy_gate_assertions(
    cohort: ValidatedEpistemicCohort, *, run_root: Path | str
) -> Iterable[CohortAssertionResult]:
    try:
        reconstructed = validate_epistemic_cohort(
            run_id=cohort.run_id, rows=cohort.rows, run_root=run_root
        )
    except CohortValidationError:
        raise
    if reconstructed != cohort:
        raise CohortValidationError("cohort signal disposition differs after evidence replay")
    for row in reconstructed.rows:
        if row.provider in CONTROL_PROVIDERS:
            continue
        for cell in row.assertions:
            if cell.signal_disposition == "product_signal":
                yield cell


def persist_validated_cohort(
    path: Path | str, cohort: ValidatedEpistemicCohort, *, run_root: Path | str
) -> Path:
    if not isinstance(cohort, ValidatedEpistemicCohort):
        raise TypeError("persist requires a ValidatedEpistemicCohort")
    try:
        reconstructed = validate_epistemic_cohort(
            run_id=cohort.run_id, rows=cohort.rows, run_root=run_root
        )
    except CohortValidationError as exc:
        raise ValueError("cannot persist a semantically unvalidated cohort") from exc
    if reconstructed != cohort:
        raise ValueError("cannot persist a semantically unvalidated cohort")
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".tmp")
    temporary.write_text(
        json.dumps(cohort.model_dump(mode="json"), sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(destination)
    return destination


def load_validated_cohort(
    path: Path | str, *, run_root: Path | str
) -> ValidatedEpistemicCohort:
    try:
        cohort = ValidatedEpistemicCohort.model_validate_json(Path(path).read_bytes())
        reconstructed = validate_epistemic_cohort(
            run_id=cohort.run_id, rows=cohort.rows, run_root=run_root
        )
    except Exception as exc:  # noqa: BLE001
        raise ValueError("stored artifact is not a validated epistemic cohort") from exc
    if reconstructed != cohort:
        raise ValueError("stored artifact is not a semantically validated epistemic cohort")
    return cohort


__all__ = [
    "CohortAssertionResult",
    "CohortExpectationIdentity",
    "CohortValidationError",
    "EpistemicCohortRow",
    "ValidatedEpistemicCohort",
    "iter_strategy_gate_assertions",
    "load_validated_cohort",
    "persist_validated_cohort",
    "validate_epistemic_cohort",
]
