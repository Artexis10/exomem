"""The frozen catastrophic set and what a failure in it does to a row.

PREREGISTRATION.md §3 named six invariants whose failure is an integrity
failure rather than a lost point: retired state served as current, destroyed
history, an unresolvable evidence path under a promoted conclusion, a silently
flattened contradiction, cross-case residue, and an ignored authoritative
external edit. Amendment sequence 1 adds a seventh, a refuted hypothesis
remaining retrievable at full standing. Each is a harm no retrieval or answer
excellence can offset, so a failure suppresses every aggregate for that
provider rather than averaging into one.

Only ``fail`` counts. ``not_applicable``, ``unsupported`` and ``blocked`` are
statements about observability, and turning them into integrity failures would
punish honest declarations — the same reason
:mod:`membench.scoring.health` never converts an underivable metric to a zero.
"""

from __future__ import annotations

from collections.abc import Iterable

from .assertions import AssertionResult

#: PREREGISTRATION.md §3. Frozen with the pre-registration hash.
CATASTROPHIC_ASSERTIONS: frozenset[str] = frozenset(
    {
        "no_retired_state_served_as_current",
        "prior_revision_retained",
        "evidence_path_resolves",
        "contradiction_not_flattened",
        "no_cross_case_residue",
        "external_edit_authoritative_within",
        # Amendment sequence 1, acknowledged 2026-08-15: losing a refuted
        # hypothesis is an integrity failure in exactly the way losing a prior
        # revision is, so f18 joins the set rather than merely scoring low.
        "refuted_retrievable_at_full_standing",
    }
)

#: Provider row status vocabulary.
PROVIDER_OK = "OK"
PROVIDER_INTEGRITY_FAIL = "INTEGRITY_FAIL"


def is_catastrophic(result: AssertionResult) -> bool:
    """True iff this result is a failure of a registered catastrophic assertion."""

    return result.outcome == "fail" and result.name in CATASTROPHIC_ASSERTIONS


def catastrophic_failures(
    results: Iterable[AssertionResult],
) -> tuple[AssertionResult, ...]:
    """The catastrophic failures among ``results``, in input order."""

    return tuple(result for result in results if is_catastrophic(result))


def integrity_status(results: Iterable[AssertionResult]) -> str:
    """``INTEGRITY_FAIL`` when any catastrophic assertion failed, else ``OK``."""

    return PROVIDER_INTEGRITY_FAIL if catastrophic_failures(results) else PROVIDER_OK
