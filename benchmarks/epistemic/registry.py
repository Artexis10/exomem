"""The frozen assertion registry.

The 18 names below are pre-registered in ``PREREGISTRATION.md`` §2 and were
committed before any competitor was run by this programme. The registry is a
closed set on purpose: a scenario that names anything else fails to load, which
is what stops the suite from growing an assertion to fit a result it wanted.

``PREREGISTERED_ASSERTIONS`` mirrors §2 in code so the mapping can be checked
without file I/O at import time; ``tests/test_epistemic_registry.py`` parses the
markdown and fails on any drift between the two.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from types import MappingProxyType

from . import assertions
from .assertions import AssertionContext, AssertionResult

AssertionFn = Callable[[AssertionContext], AssertionResult]


class RegistryError(LookupError):
    """Raised when a name is not in the frozen registry. A load-time error."""


#: PREREGISTRATION.md §2, in file order. Frozen with the pre-registration hash.
PREREGISTERED_ASSERTIONS: tuple[str, ...] = (
    "exactly_one_current_revision",
    "no_retired_state_served_as_current",
    "prior_revision_retained",
    "revision_links_to_predecessor",
    "evidence_path_exists",
    "evidence_path_resolves",
    "contradiction_visible",
    "contradiction_not_flattened",
    "decision_distinguishable_from_hypothesis",
    "open_question_queryable",
    "uncertainty_declared",
    "review_state_durable",
    "review_reopens_on_material_change",
    "review_stays_closed_on_irrelevant_change",
    "external_edit_authoritative_within",
    "export_reconstructs_state",
    "dependent_conclusions_surfaced_for_review",
    "no_cross_case_residue",
)


#: PREREGISTRATION.md §1, in file order: ``(family_id, family_name)``.
#: Load-bearing — the scenario loader rejects an unregistered ``family_id``,
#: and ``tests/test_epistemic_registry.py`` drift-tests this against the table.
PREREGISTERED_FAMILIES: tuple[tuple[str, str], ...] = (
    ("f01", "explicit_correction"),
    ("f02", "implicit_staleness"),
    ("f03", "conflicting_sources"),
    ("f04", "source_quality_asymmetry"),
    ("f05", "supersession_lineage"),
    ("f06", "evidence_before_belief"),
    ("f07", "decision_vs_hypothesis"),
    ("f08", "modeled_ignorance"),
    ("f09", "abstention_insufficient_support"),
    ("f10", "downstream_impact"),
    ("f11", "triage_invalidation"),
    ("f12", "external_canonical_edit"),
    ("f13", "engine_off_portability"),
    ("f14", "cross_agent_continuation"),
)

PREREGISTERED_FAMILY_IDS: frozenset[str] = frozenset(
    family_id for family_id, _name in PREREGISTERED_FAMILIES
)

#: Assertions whose semantics compare two *named items*. A scenario expectation
#: must declare both ``subject`` and ``counterpart``; otherwise the assertion
#: silently degrades to a weaker snapshot-wide reading, which is exactly the
#: kind of quiet downgrade a comparative benchmark cannot afford.
REQUIRES_ITEM_PAIR: frozenset[str] = frozenset(
    {
        "contradiction_visible",
        "contradiction_not_flattened",
        "decision_distinguishable_from_hypothesis",
    }
)

#: Assertions evaluated over a *snapshot pair*. The trajectory must actually
#: take two snapshots at or before the phase that expects them.
REQUIRES_SNAPSHOT_PAIR: frozenset[str] = frozenset(
    {
        "review_state_durable",
        "review_reopens_on_material_change",
        "review_stays_closed_on_irrelevant_change",
        "external_edit_authoritative_within",
        "export_reconstructs_state",
        "dependent_conclusions_surfaced_for_review",
    }
)


def _build_registry() -> Mapping[str, AssertionFn]:
    mapping: dict[str, AssertionFn] = {}
    for name in PREREGISTERED_ASSERTIONS:
        fn = getattr(assertions, name, None)
        if fn is None or not callable(fn):
            raise RegistryError(f"pre-registered assertion has no implementation: {name}")
        mapping[name] = fn
    return MappingProxyType(mapping)


#: name -> deterministic callable. Read-only; the set never grows at runtime.
ASSERTION_REGISTRY: Mapping[str, AssertionFn] = _build_registry()


def resolve(name: str) -> AssertionFn:
    """Return the callable for ``name`` or raise :class:`RegistryError`."""

    try:
        return ASSERTION_REGISTRY[name]
    except KeyError:
        raise RegistryError(
            f"unknown assertion: {name!r} is not in the pre-registered registry "
            f"({len(ASSERTION_REGISTRY)} names)"
        ) from None


def registered_names() -> tuple[str, ...]:
    """The frozen names, in pre-registration order."""

    return PREREGISTERED_ASSERTIONS


def parse_preregistered_assertions(text: str) -> tuple[str, ...]:
    """Extract the §2 assertion names from the pre-registration markdown.

    Kept here rather than in the test so the parse rule is part of the engine
    and any change to it is reviewed alongside the registry.
    """

    marker = "## 2. Assertion registry"
    try:
        start = text.index(marker)
        fence = text.index("```", start) + len("```")
        end = text.index("```", fence)
    except ValueError as error:
        raise RegistryError(
            "pre-registration is missing a fenced assertion block under §2"
        ) from error
    return tuple(text[fence:end].split())


def parse_preregistered_families(text: str) -> tuple[tuple[str, str], ...]:
    """Extract the §1 family table rows as ``(family_id, family_name)``."""

    marker = "## 1. Scenario families"
    try:
        start = text.index(marker)
        end = text.index("## 2.", start)
    except ValueError as error:
        raise RegistryError("pre-registration is missing the §1 family table") from error
    rows: list[tuple[str, str]] = []
    for line in text[start:end].splitlines():
        match = re.match(r"^\|\s*(f\d{2})\s*\|\s*([a-z0-9_]+)\s*\|", line.strip())
        if match is not None:
            rows.append((match.group(1), match.group(2)))
    if not rows:
        raise RegistryError("pre-registration §1 family table has no parsable rows")
    return tuple(rows)
