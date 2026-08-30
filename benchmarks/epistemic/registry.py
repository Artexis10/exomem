"""The frozen assertion registry.

The 35 names below are pre-registered in ``PREREGISTRATION.md`` §2 — eighteen
committed before any competitor was run by this programme, six added by the
2026-08 loop-closure amendment, nine added by the 2026-08 no-nudge amendment,
and two added by the 2026-08 lifecycle-replay amendment, all through the
governed §7 path. The registry is a closed set on
purpose: a scenario that names anything else fails to load, which is what stops
the suite from growing an assertion to fit a result it wanted.

Registration is not release. The families each amendment introduced stay
withheld from comparative runs until its receipt is acknowledged; that gate
lives in :mod:`epistemic.amendments` and fires at the same load-time choke
point this registry does. Sequence 1 is acknowledged; sequences 2 and 3 are
not, so f20-f26 and f27 are registered and withheld at once.

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
    # Added by the 2026-08 loop-closure amendment (§7).
    "due_prediction_surfaced",
    "verdict_state_retrievable",
    "divergence_surfaced_without_mutation",
    "support_collapse_inspectable",
    "refuted_retrievable_at_full_standing",
    "loop_journey_state_coherent",
    # Added by the 2026-08 no-nudge amendment (§7), sequence 2.
    # ``signal_absence_checked_across_all_surfaces`` is deliberately FIRST: it is
    # the anti-vacuity meta-predicate every quiet assertion in f20-f26 composes,
    # and registering it ahead of the families that depend on it keeps the
    # reading order of §2 the same as the dependency order in code.
    "signal_absence_checked_across_all_surfaces",
    "structural_signal_surfaced_within_budget",
    "entity_candidate_surfaced_from_recurrence",
    "contradiction_surfaced_unprompted",
    "dismissal_respected_across_passes",
    "counter_emission_not_repeated_per_write",
    "continuation_packet_reconstructs_session",
    "restructure_signal_cleared_by_state_change",
    "due_state_block_present_in_carrier",
    # Added by the 2026-08 lifecycle-replay amendment (§7), sequence 3. The two
    # are a *pair*: coverage without its false-write dual would reward a product
    # that wrote something for every utterance, so they are registered together
    # and reported together.
    "lifecycle_consequence_landed_unprompted",
    "no_structured_write_beyond_expectation",
)

#: Quiet assertions: every one composes
#: :func:`~epistemic.assertions.signal_absence_checked_across_all_surfaces`, so
#: a negative control can never pass by relocating a nag to an unchecked surface
#: or by projecting nothing at all.
#:
#: Membership is not a matter of remembering to edit this literal. Each
#: predicate is marked with ``@claims_absence`` where it is defined, and
#: ``tests/test_epistemic_no_nudge_families.py`` asserts this set equals exactly
#: the marked ones *and* that every member propagates the meta-predicate when it
#: is made to refuse. The literal is kept because the rest of the registry is
#: hand-mirrored for the same reason — no import-time introspection of the
#: assertion module's decorators is needed to read what the governance says.
COMPOSES_ABSENCE_META: frozenset[str] = frozenset(
    {
        "signal_absence_checked_across_all_surfaces",
        "dismissal_respected_across_passes",
        "restructure_signal_cleared_by_state_change",
    }
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
    # Added by the 2026-08 loop-closure amendment (§7).
    ("f15", "prediction_window"),
    ("f16", "plan_record_linkage"),
    ("f17", "derivation_collapse"),
    ("f18", "negative_result_retention"),
    ("f19", "loop_composite"),
    # Added by the 2026-08 no-nudge amendment (§7), sequence 2.
    ("f20", "structural_emergence"),
    ("f21", "entity_emergence"),
    ("f22", "unsolicited_contradiction"),
    ("f23", "dismissal_respect"),
    ("f24", "fresh_session_reconstruction"),
    ("f25", "restructure_lifecycle"),
    ("f26", "hookless_episode_carrier"),
    # Added by the 2026-08 lifecycle-replay amendment (§7), sequence 3.
    ("f27", "lifecycle_routing_replay"),
)

PREREGISTERED_FAMILY_IDS: frozenset[str] = frozenset(
    family_id for family_id, _name in PREREGISTERED_FAMILIES
)

#: ``family_id -> amendment sequence that introduced it``, mirroring §7.
#:
#: Being *pre-registered* and being *released* are two different facts, and this
#: mapping is what keeps them apart. Before the amendment, f15-f19 were refused
#: by the scenario loader for the incidental reason that §1 did not know them;
#: registering them above removes that accident, so the receipt has to withhold
#: them on purpose until the founder acknowledges it — see
#: :mod:`epistemic.amendments`. The mapping is hand-mirrored from the document
#: for the same reason ``PREREGISTERED_ASSERTIONS`` is (no file or Git I/O at
#: import time) and is drift-tested against the derived receipt chain in
#: ``tests/test_epistemic_amendment_governance.py``.
AMENDMENT_INTRODUCED_FAMILIES: Mapping[str, int] = MappingProxyType(
    {
        "f15": 1,
        "f16": 1,
        "f17": 1,
        "f18": 1,
        "f19": 1,
        # Sequence 2 (no-nudge). Pending founder acknowledgment, so every one of
        # these is withheld from comparative runs, scores and claims — which is
        # the entire observable difference between being registered and being
        # released, and the reason f20-f22 being expected-red is a falsification
        # target rather than a CI failure.
        "f20": 2,
        "f21": 2,
        "f22": 2,
        "f23": 2,
        "f24": 2,
        "f25": 2,
        "f26": 2,
        # Sequence 3 (lifecycle replay). Acknowledged 2026-08-30, releasing
        # f27 the same way sequence 1's families were released — the development
        # run recorded before acknowledgment remains evidence about the harness
        # and that runtime, never a comparative claim.
        "f27": 3,
    }
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
        # f22 surfaces a *pair* — the invalidated conclusion and the evidence
        # that invalidates it — so a scenario that names only one of them would
        # be asserting something weaker than the family claims.
        "contradiction_surfaced_unprompted",
    }
)

#: Assertions that are meaningless without a named subject. A quiet assertion in
#: particular proves silence *about something*; letting it run subject-less would
#: turn "no signal names this twin" into "no signal exists", which is a different
#: and far weaker claim that an empty snapshot would satisfy.
REQUIRES_SUBJECT: frozenset[str] = frozenset(
    {
        # f27's pair reads its expectation out of the corpus the subject names.
        # Subject-less both block with the reason, but blocking at evaluation is
        # late: the mistake is visible at load, and a scenario that forgot the
        # corpus id should be refused there rather than produce a run whose two
        # assertions are both unscoreable.
        "lifecycle_consequence_landed_unprompted",
        "no_structured_write_beyond_expectation",
        "signal_absence_checked_across_all_surfaces",
        "structural_signal_surfaced_within_budget",
        "entity_candidate_surfaced_from_recurrence",
        "contradiction_surfaced_unprompted",
        "dismissal_respected_across_passes",
        "restructure_signal_cleared_by_state_change",
    }
)

#: Assertions evaluated over a *snapshot pair*. The trajectory must actually
#: take two snapshots at or before the phase that expects them.
REQUIRES_SNAPSHOT_PAIR: frozenset[str] = frozenset(
    {
        # f27's false-write dual diffs pages against the seeded vault, so the
        # trajectory owes a snapshot taken before the first agent turn. Without
        # it a scaffold page the harness itself laid would be scored as a page
        # the agent wrote.
        "no_structured_write_beyond_expectation",
        "review_state_durable",
        "review_reopens_on_material_change",
        "review_stays_closed_on_irrelevant_change",
        "external_edit_authoritative_within",
        "export_reconstructs_state",
        "dependent_conclusions_surfaced_for_review",
        # f16 proves the plan was not auto-mutated, and f19 proves the journey
        # survived a restart. Both are statements about a transition, so a
        # trajectory that took one snapshot cannot support either.
        "divergence_surfaced_without_mutation",
        "loop_journey_state_coherent",
        # f23 compares a recorded dismissal against a later maintenance pass:
        # "the fingerprint did not come back" is a statement about a transition,
        # and a single snapshot cannot support it.
        "dismissal_respected_across_passes",
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
