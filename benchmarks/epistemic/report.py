"""Offline renderer for stored, validated Epistemic State Bench cohorts."""

from __future__ import annotations

import json
from pathlib import Path

from protocol.offline import offline_guard

from .catastrophic import CATASTROPHIC_ASSERTIONS
from .cohort import (
    CohortValidationError,
    ValidatedEpistemicCohort,
    _replay_cell,
    _validate_shape,
    validate_epistemic_cohort,
)


def _escape(value: object) -> str:
    return str(value).replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ")


def _render_validated_cohort(
    cohort: ValidatedEpistemicCohort, *, run_root: Path
) -> str:
    failures: list[str] = []
    verified: set[tuple[int, int]] = set()
    for row_index, row in enumerate(cohort.rows):
        for cell_index, cell in enumerate(row.assertions):
            try:
                _replay_cell(run_root=run_root, row=row, cell=cell)
            except CohortValidationError as exc:
                failures.append(
                    f"provider={row.provider} variant={row.variant}: "
                    f"{cell.identity.assertion}: {exc}"
                )
            else:
                verified.add((row_index, cell_index))

    if not failures:
        reconstructed = validate_epistemic_cohort(
            run_id=cohort.run_id,
            rows=cohort.rows,
            run_root=run_root,
        )
        if reconstructed != cohort:
            raise ValueError("stored artifact is not a semantically validated epistemic cohort")

    lines = ["# Epistemic State Bench", "", f"Run: `{_escape(cohort.run_id)}`", ""]
    if failures:
        lines.extend(
            [
                "## WITHHELD",
                "",
                "Product findings are withheld: unreplayable assertion evidence.",
                "",
            ]
        )
        lines.extend(f"- {_escape(failure)}" for failure in failures)
        lines.append("")
        return "\n".join(lines)
    else:
        catastrophes = [
            (row, cell)
            for row in cohort.rows
            if row.provider not in {"grep-markdown", "no-memory"}
            for cell in row.assertions
            if cell.result.outcome == "fail"
            and cell.identity.assertion in CATASTROPHIC_ASSERTIONS
        ]
        if catastrophes:
            lines.extend(["## INTEGRITY FAIL", ""])
            for row, cell in catastrophes:
                assert cell.evidence_ref is not None
                lines.append(
                    f"- {_escape(row.provider)}: {_escape(cell.identity.assertion)} "
                    f"— artifact `{_escape(cell.evidence_ref.path)}`"
                )
            lines.append("")
        visible_cells = {
            (row_index, cell_index)
            for row_index, row in enumerate(cohort.rows)
            for cell_index, _cell in enumerate(row.assertions)
        }

    lines.extend(
        [
            "## Validated cohort",
            "",
            "| Provider | Variant | Scenario | Assertion | Outcome | Signal disposition | Evidence artifact |",
            "| --- | --- | --- | --- | --- | --- | --- |",
        ]
    )
    for row_index, row in enumerate(cohort.rows):
        for cell_index, cell in enumerate(row.assertions):
            if (row_index, cell_index) not in visible_cells:
                continue
            evidence = cell.evidence_ref.path if cell.evidence_ref else "—"
            lines.append(
                "| "
                + " | ".join(
                    _escape(value)
                    for value in (
                        row.provider,
                        row.variant,
                        cell.identity.scenario_id,
                        cell.identity.assertion,
                        cell.result.outcome,
                        cell.signal_disposition,
                        evidence,
                    )
                )
                + " |"
            )
    lines.append("")
    return "\n".join(lines)


def _load_stored_cohort_without_trusting_claims(path: Path | str) -> ValidatedEpistemicCohort:
    try:
        cohort = ValidatedEpistemicCohort.model_validate_json(Path(path).read_bytes())
        _validate_shape(cohort.rows)
    except Exception as exc:  # noqa: BLE001
        raise ValueError("stored artifact is not a validated epistemic cohort") from exc
    return cohort


def _contains_missing_evidence(path: Path | str) -> bool:
    try:
        payload = json.loads(Path(path).read_bytes())
        rows = payload.get("rows", [])
        return bool(rows) and any(
            cell.get("evidence_ref") is None
            for row in rows
            for cell in row.get("assertions", [])
        )
    except Exception:  # noqa: BLE001 - only selects safe withholding behavior
        return False


def _render_missing_evidence() -> str:
    return "\n".join(
        (
            "# Epistemic State Bench",
            "",
            "## WITHHELD",
            "",
            "Product findings are withheld: unreplayable assertion evidence.",
            "",
        )
    )


def render_epistemic_report(
    cohort_path: Path | str, *, run_root: Path | str, offline: bool = True
) -> str:
    if not isinstance(cohort_path, (Path, str)):
        raise TypeError("renderer requires a stored validated cohort path")
    if offline:
        with offline_guard():
            try:
                cohort = _load_stored_cohort_without_trusting_claims(cohort_path)
            except ValueError:
                if _contains_missing_evidence(cohort_path):
                    return _render_missing_evidence()
                raise
            return _render_validated_cohort(cohort, run_root=Path(run_root))
    try:
        cohort = _load_stored_cohort_without_trusting_claims(cohort_path)
    except ValueError:
        if _contains_missing_evidence(cohort_path):
            return _render_missing_evidence()
        raise
    return _render_validated_cohort(cohort, run_root=Path(run_root))


__all__ = ["render_epistemic_report"]
