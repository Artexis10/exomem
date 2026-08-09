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
