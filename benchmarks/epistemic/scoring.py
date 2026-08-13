"""Family-level assembly and the two suppression rules.

There is deliberately **no aggregate number anywhere in this module** — no
score, no total, no overall. A single figure would let a catastrophic integrity
failure be averaged against retrieval excellence, which is precisely the move
the pre-registration forbids. What the result models carry instead is the
per-assertion verdict, a row status, and two booleans that say when a
comparison is allowed at all:

- **Integrity suppression.** Any catastrophic failure anywhere in the run marks
  that provider ``INTEGRITY_FAIL`` and stamps every one of its family rows with
  the same status, so no row of that provider can be read as a clean result.
- **N/A poisoning.** A ``not_applicable`` from *any* provider excludes that
  family from comparative claims for *all* providers. Otherwise the provider
  that honestly declares a missing capability would be compared against
  providers measured on a different question.

Both rules record their reason in ``RunResult.exclusions`` so a renderer can
print the exclusion rather than silently dropping a row.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Literal

from pydantic import Field

from .assertions import AssertionResult
from .catastrophic import PROVIDER_INTEGRITY_FAIL, PROVIDER_OK, catastrophic_failures
from .snapshot import StrictModel

#: ``scored`` = evaluated normally; ``INTEGRITY_FAIL`` = suppressed by §3;
#: ``blocked`` = an environment fault stopped evaluation.
FamilyStatus = Literal["scored", "INTEGRITY_FAIL", "blocked"]

ProviderStatus = Literal["OK", "INTEGRITY_FAIL"]


class FamilyResult(StrictModel):
    """One provider variant's verdicts for one scenario family."""

    family_id: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    variant: str = "native"
    assertion_results: tuple[AssertionResult, ...] = ()
    status: FamilyStatus = "scored"
    comparable: bool = True
    catastrophic_failures: tuple[str, ...] = ()

    def outcomes(self) -> tuple[str, ...]:
        return tuple(result.outcome for result in self.assertion_results)


class RunResult(StrictModel):
    """Every family row plus the suppression bookkeeping that governs reading."""

    run_id: str = Field(min_length=1)
    families: tuple[FamilyResult, ...] = ()
    #: provider -> ``OK`` | ``INTEGRITY_FAIL``
    provider_status: dict[str, ProviderStatus] = Field(default_factory=dict)
    #: family_id -> whether the family may back a comparative claim at all
    family_comparability: dict[str, bool] = Field(default_factory=dict)
    suppressed_providers: tuple[str, ...] = ()
    exclusions: tuple[str, ...] = ()


def assemble_family(
    *,
    family_id: str,
    provider: str,
    variant: str = "native",
    assertion_results: Sequence[AssertionResult] = (),
) -> FamilyResult:
    """Score one family row: catastrophic first, then blocked, then comparable."""

    failures = catastrophic_failures(assertion_results)
    if failures:
        status: FamilyStatus = "INTEGRITY_FAIL"
    elif any(result.outcome == "blocked" for result in assertion_results):
        status = "blocked"
    else:
        status = "scored"
    comparable = not any(result.outcome == "not_applicable" for result in assertion_results)
    return FamilyResult(
        family_id=family_id,
        provider=provider,
        variant=variant,
        assertion_results=tuple(assertion_results),
        status=status,
        comparable=comparable,
        catastrophic_failures=tuple(failure.name for failure in failures),
    )


def assemble_run(*, run_id: str, families: Iterable[FamilyResult]) -> RunResult:
    """Apply integrity suppression and N/A poisoning across the whole run."""

    rows = tuple(families)
    exclusions: list[str] = []

    suppressed: set[str] = set()
    for row in rows:
        for name in row.catastrophic_failures:
            suppressed.add(row.provider)
            exclusions.append(
                f"{row.provider}/{row.variant}: INTEGRITY_FAIL — catastrophic assertion "
                f"{name} failed in family {row.family_id}; every aggregate suppressed"
            )

    poisoned: dict[str, list[str]] = {}
    for row in rows:
        for result in row.assertion_results:
            if result.outcome == "not_applicable":
                poisoned.setdefault(row.family_id, []).append(
                    f"{row.provider}/{row.variant}:{result.name}"
                )
    for family_id in sorted(poisoned):
        sources = ", ".join(sorted(poisoned[family_id]))
        exclusions.append(
            f"{family_id}: excluded from comparative claims for ALL providers — "
            f"not_applicable from {sources}"
        )

    families_seen = {row.family_id for row in rows}
    comparability = {
        family_id: family_id not in poisoned for family_id in sorted(families_seen)
    }

    adjusted = tuple(
        row.model_copy(
            update={
                "status": "INTEGRITY_FAIL" if row.provider in suppressed else row.status,
                "comparable": comparability.get(row.family_id, True) and row.comparable,
            }
        )
        for row in rows
    )

    provider_status: dict[str, ProviderStatus] = {}
    for row in rows:
        provider_status[row.provider] = (
            PROVIDER_INTEGRITY_FAIL if row.provider in suppressed else PROVIDER_OK
        )

    return RunResult(
        run_id=run_id,
        families=adjusted,
        provider_status=provider_status,
        family_comparability=comparability,
        suppressed_providers=tuple(sorted(suppressed)),
        exclusions=tuple(exclusions),
    )
