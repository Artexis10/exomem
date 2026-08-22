"""Discrimination tests: every registered assertion separates pass from fail.

Each of the 33 pre-registered assertions gets at least one hand-built passing
snapshot (or snapshot pair) and one failing one. A registry entry with no
discriminating pair is a hole, so the coverage test below is as load-bearing as
the pairs themselves.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from epistemic.assertions import AssertionContext, AssertionResult
from epistemic.registry import ASSERTION_REGISTRY, PREREGISTERED_ASSERTIONS, resolve
from epistemic.snapshot import (
    DECLARABLE_FIELDS,
    EpistemicStateSnapshot,
    FieldDeclaration,
    ProjectorMeta,
    Relation,
    StateItem,
)

PROJECTOR = ProjectorMeta(
    name="fixture-projector",
    version="0.1.0",
    author="benchmark-harness",
    endpoints_used=("fixture:in-memory",),
    loc=1,
)


def declarations(**overrides: str) -> tuple[FieldDeclaration, ...]:
    statuses = {field: "declared" for field in DECLARABLE_FIELDS}
    statuses.update(overrides)
    return tuple(
        FieldDeclaration(
            field=field,
            status=status,
            evidence=f"benchmarks/epistemic/PREREGISTRATION.md:39 ({field})",
        )
        for field, status in sorted(statuses.items())
    )


def item(item_id: str, **kwargs: object) -> StateItem:
    kwargs.setdefault("kind", "claim")
    kwargs.setdefault("title", item_id)
    kwargs.setdefault("text", f"body of {item_id}")
    return StateItem(id=item_id, **kwargs)  # type: ignore[arg-type]


def snapshot(
    items: tuple[StateItem, ...],
    *,
    relations: tuple[Relation, ...] = (),
    decls: tuple[FieldDeclaration, ...] | None = None,
    phase: str = "p1",
    taken_at: str = "2026-01-01T00:00:00Z",
) -> EpistemicStateSnapshot:
    return EpistemicStateSnapshot(
        provider="fixture",
        variant="native",
        phase=phase,
        taken_at=taken_at,
        items=items,
        relations=relations,
        declarations=declarations() if decls is None else decls,
        projector=PROJECTOR,
        completeness_notes="hand-built discrimination fixture",
    )


# --------------------------------------------------------------------------
# Per-assertion pass/fail context factories.
# --------------------------------------------------------------------------


def _revision_pair(*, both_current: bool) -> AssertionContext:
    old = item(
        "budget-v1",
        current="yes" if both_current else "no",
        retired_reason=None if both_current else "superseded by budget-v2",
        revision_chain_id="chain-budget",
        revision_index=0,
    )
    new = item(
        "budget-v2",
        current="yes",
        revision_of="budget-v1",
        revision_chain_id="chain-budget",
        revision_index=1,
    )
    return AssertionContext(snapshot=snapshot((old, new)), subject="chain-budget")


def exactly_one_current_revision_pass() -> AssertionContext:
    return _revision_pair(both_current=False)


def exactly_one_current_revision_fail() -> AssertionContext:
    return _revision_pair(both_current=True)


def no_retired_state_served_as_current_pass() -> AssertionContext:
    ctx = _revision_pair(both_current=False)
    return ctx.replace(served_items=("budget-v2",))


def no_retired_state_served_as_current_fail() -> AssertionContext:
    ctx = _revision_pair(both_current=False)
    return ctx.replace(served_items=("budget-v1",))


def prior_revision_retained_pass() -> AssertionContext:
    return _revision_pair(both_current=False).replace(subject="budget-v2")


def prior_revision_retained_fail() -> AssertionContext:
    """B1: history destroyed, but an unrelated retired note is still around.

    The declared predecessor is gone and the only retained item belongs to a
    different lineage. "Some retired item exists somewhere" is not history
    retention for this subject.
    """

    orphaned_successor = item("budget-v2", current="yes", revision_of="budget-v1")
    unrelated = item(
        "unrelated-old-note",
        current="no",
        retired_reason="archived; nothing to do with the budget lineage",
    )
    return AssertionContext(
        snapshot=snapshot((orphaned_successor, unrelated)), subject="budget-v2"
    )


def revision_links_to_predecessor_pass() -> AssertionContext:
    return _revision_pair(both_current=False).replace(subject="budget-v2")


def revision_links_to_predecessor_fail() -> AssertionContext:
    old = item("budget-v1", current="no", retired_reason="replaced")
    new = item("budget-v2", current="yes", text="A fresh restatement with no back reference.")
    return AssertionContext(snapshot=snapshot((old, new)), subject="budget-v2")


def _evidence_snapshot(*, resolvable: bool) -> EpistemicStateSnapshot:
    source = item("src-1", kind="raw_source", current="yes", authored_by="human")
    claim = item(
        "claim-1",
        kind="claim",
        current="yes",
        cites=("src-1",) if resolvable else ("src-missing",),
    )
    return snapshot((source, claim))


def evidence_path_exists_pass() -> AssertionContext:
    return AssertionContext(snapshot=_evidence_snapshot(resolvable=True))


def evidence_path_exists_fail() -> AssertionContext:
    source = item("src-1", kind="raw_source", current="yes")
    claim = item("claim-1", kind="claim", current="yes")
    return AssertionContext(snapshot=snapshot((source, claim)))


def evidence_path_resolves_pass() -> AssertionContext:
    return AssertionContext(snapshot=_evidence_snapshot(resolvable=True))


def evidence_path_resolves_fail() -> AssertionContext:
    return AssertionContext(snapshot=_evidence_snapshot(resolvable=False))


def contradiction_visible_pass() -> AssertionContext:
    left = item("claim-a", current="yes", contradicts=("claim-b",))
    right = item("claim-b", current="yes", contradicts=("claim-a",))
    return AssertionContext(
        snapshot=snapshot((left, right)), subject="claim-a", counterpart="claim-b"
    )


def contradiction_visible_fail() -> AssertionContext:
    left = item("claim-a", current="yes")
    right = item("claim-b", current="yes")
    return AssertionContext(
        snapshot=snapshot((left, right)), subject="claim-a", counterpart="claim-b"
    )


def contradiction_not_flattened_pass() -> AssertionContext:
    return contradiction_visible_pass()


def contradiction_not_flattened_fail() -> AssertionContext:
    survivor = item("claim-a", current="yes")
    return AssertionContext(
        snapshot=snapshot((survivor,)), subject="claim-a", counterpart="claim-b"
    )


def decision_distinguishable_from_hypothesis_pass() -> AssertionContext:
    decision = item("decide-1", kind="decision", current="yes")
    hypothesis = item("hypo-1", kind="hypothesis", current="yes")
    return AssertionContext(
        snapshot=snapshot((decision, hypothesis)), subject="decide-1", counterpart="hypo-1"
    )


def decision_distinguishable_from_hypothesis_fail() -> AssertionContext:
    flat_a = item("decide-1", kind="claim", current="yes", locator="Notes/x", locator_kind="file")
    flat_b = item("hypo-1", kind="claim", current="yes", locator="Notes/y", locator_kind="file")
    return AssertionContext(
        snapshot=snapshot((flat_a, flat_b)), subject="decide-1", counterpart="hypo-1"
    )


def open_question_queryable_pass() -> AssertionContext:
    question = item("q-1", kind="open_question", current="yes")
    return AssertionContext(snapshot=snapshot((question,)))


def open_question_queryable_fail() -> AssertionContext:
    return AssertionContext(snapshot=snapshot((item("claim-1", current="yes"),)))


def uncertainty_declared_pass() -> AssertionContext:
    claim = item("claim-1", current="yes", uncertainty="one source only; not corroborated")
    return AssertionContext(snapshot=snapshot((claim,)), subject="claim-1")


def uncertainty_declared_fail() -> AssertionContext:
    claim = item("claim-1", current="yes")
    return AssertionContext(snapshot=snapshot((claim,)), subject="claim-1")


def _review_pair(
    *, prior_state: str, post_state: str | None, post_text: str, extra_post: bool = False
) -> AssertionContext:
    before = snapshot((item("note-1", current="yes", review_state=prior_state),), phase="pre")
    post_items = (
        item("note-1", current="yes", review_state=post_state, text=post_text),
    )
    if extra_post:
        post_items = post_items + (item("note-2", current="yes"),)
    after = snapshot(post_items, phase="post", taken_at="2026-01-02T00:00:00Z")
    return AssertionContext(snapshot=after, prior=before, subject="note-1")


def review_state_durable_pass() -> AssertionContext:
    return _review_pair(prior_state="open", post_state="open", post_text="body of note-1")


def review_state_durable_fail() -> AssertionContext:
    return _review_pair(prior_state="open", post_state=None, post_text="body of note-1")


def review_reopens_on_material_change_pass() -> AssertionContext:
    return _review_pair(
        prior_state="dismissed", post_state="open", post_text="a materially rewritten body"
    )


def review_reopens_on_material_change_fail() -> AssertionContext:
    return _review_pair(
        prior_state="dismissed", post_state="dismissed", post_text="a materially rewritten body"
    )


def review_stays_closed_on_irrelevant_change_pass() -> AssertionContext:
    return _review_pair(
        prior_state="dismissed",
        post_state="dismissed",
        post_text="body of note-1",
        extra_post=True,
    )


def review_stays_closed_on_irrelevant_change_fail() -> AssertionContext:
    return _review_pair(
        prior_state="dismissed",
        post_state="open",
        post_text="body of note-1",
        extra_post=True,
    )


def _external_edit(*, adopted: bool) -> AssertionContext:
    before = snapshot((item("file-1", current="yes", text="the original line"),), phase="pre")
    after_text = "the externally edited line" if adopted else "the original line"
    after = snapshot(
        (item("file-1", current="yes", text=after_text),),
        phase="post",
        taken_at="2026-01-01T00:00:30Z",
    )
    return AssertionContext(
        snapshot=after,
        prior=before,
        subject="file-1",
        external_edit_at="2026-01-01T00:00:00Z",
        freshness_bound_s=60.0,
    )


def external_edit_authoritative_within_pass() -> AssertionContext:
    return _external_edit(adopted=True)


def external_edit_authoritative_within_fail() -> AssertionContext:
    return _external_edit(adopted=False)


def _export_pair(*, complete: bool) -> AssertionContext:
    live_items = (
        item("src-1", kind="raw_source", current="yes"),
        item("claim-1", current="yes", cites=("src-1",), revision_of=None),
    )
    live = snapshot(live_items, phase="live")
    export_items = live_items if complete else live_items[:1]
    derived = snapshot(export_items, phase="export")
    return AssertionContext(snapshot=derived, prior=live, tolerance=0.0)


def export_reconstructs_state_pass() -> AssertionContext:
    return _export_pair(complete=True)


def export_reconstructs_state_fail() -> AssertionContext:
    return _export_pair(complete=False)


def _downstream_pair(*, flagged: bool) -> AssertionContext:
    before = snapshot(
        (
            item("src-1", kind="raw_source", current="yes"),
            item("claim-1", current="yes", cites=("src-1",)),
        ),
        phase="pre",
    )
    dependent = item(
        "claim-1",
        current="yes",
        cites=("src-1",),
        review_state="open" if flagged else None,
    )
    after = snapshot(
        (
            item(
                "src-1",
                kind="raw_source",
                current="no",
                retired_reason="superseded by a corrected capture",
            ),
            dependent,
        ),
        phase="post",
        taken_at="2026-01-02T00:00:00Z",
    )
    return AssertionContext(snapshot=after, prior=before)


def dependent_conclusions_surfaced_for_review_pass() -> AssertionContext:
    return _downstream_pair(flagged=True)


def dependent_conclusions_surfaced_for_review_fail() -> AssertionContext:
    return _downstream_pair(flagged=False)


def no_cross_case_residue_pass() -> AssertionContext:
    return AssertionContext(snapshot=snapshot((item("claim-1", current="yes"),)), foreign_case_hits=())


def no_cross_case_residue_fail() -> AssertionContext:
    return AssertionContext(
        snapshot=snapshot((item("claim-1", current="yes"),)),
        foreign_case_hits=("canary-cross-case-0123456789abcdef",),
    )


# --------------------------------------------------------------------------
# Loop-closure families f15-f19 (PREREGISTRATION §7, 2026-08 amendment).
# --------------------------------------------------------------------------


def due_prediction_surfaced_pass() -> AssertionContext:
    prediction = item(
        "pred-latency",
        kind="hypothesis",
        raw={"prediction": "pred-latency", "due": "overdue"},
    )
    return AssertionContext(
        snapshot=snapshot((prediction,)), served_items=("pred-latency",)
    )


def due_prediction_surfaced_fail() -> AssertionContext:
    """The window elapsed, but nothing says so and nothing queues it."""

    prediction = item(
        "pred-latency",
        kind="hypothesis",
        raw={"prediction": "pred-latency", "due": "2026-01-01"},
    )
    return AssertionContext(snapshot=snapshot((prediction,)))


def verdict_state_retrievable_pass() -> AssertionContext:
    hypothesis = item("hyp-cache", kind="hypothesis", raw={"verdict": "refuted"})
    return AssertionContext(snapshot=snapshot((hypothesis,)))


def verdict_state_retrievable_fail() -> AssertionContext:
    hypothesis = item("hyp-cache", kind="hypothesis", raw={"note": "still open"})
    return AssertionContext(snapshot=snapshot((hypothesis,)))


def _plan_pair(*, mutated: bool) -> AssertionContext:
    before = item("plan-q3", kind="container", raw={"plan": "plan-q3"})
    after = item(
        "plan-q3",
        kind="container",
        text="rewritten to match what actually happened" if mutated else "body of plan-q3",
        raw={"plan": "plan-q3"},
    )
    divergence = item("review-drift", kind="container", review_state="divergence")
    return AssertionContext(
        snapshot=snapshot((after, divergence)),
        prior=snapshot((before,), phase="p0"),
        subject="plan-q3",
    )


def divergence_surfaced_without_mutation_pass() -> AssertionContext:
    return _plan_pair(mutated=False)


def divergence_surfaced_without_mutation_fail() -> AssertionContext:
    """Surfaced, but the plan was silently rewritten to agree with the records."""

    return _plan_pair(mutated=True)


def _collapse_snapshot(*, shared_root_visible: bool) -> AssertionContext:
    source = item("src-survey", kind="raw_source")
    left = item("note-a", kind="derived_inference", cites=("src-survey",))
    right = item(
        "note-b",
        kind="derived_inference",
        cites=("src-survey",) if shared_root_visible else (),
    )
    consumer = item("claim-strong", kind="claim", cites=("note-a", "note-b"))
    return AssertionContext(
        snapshot=snapshot((source, left, right, consumer)), subject="claim-strong"
    )


def support_collapse_inspectable_pass() -> AssertionContext:
    return _collapse_snapshot(shared_root_visible=True)


def support_collapse_inspectable_fail() -> AssertionContext:
    """One support path dead-ends, so double-counting cannot be seen."""

    return _collapse_snapshot(shared_root_visible=False)


def refuted_retrievable_at_full_standing_pass() -> AssertionContext:
    refuted = item(
        "hyp-prefetch",
        kind="hypothesis",
        current="yes",
        raw={"verdict": "refuted"},
    )
    return AssertionContext(
        snapshot=snapshot((refuted,)), served_items=("hyp-prefetch",)
    )


def refuted_retrievable_at_full_standing_fail() -> AssertionContext:
    """Retained on disk, but demoted for the crime of being a negative result."""

    refuted = item(
        "hyp-prefetch",
        kind="hypothesis",
        current="no",
        retired_reason="refuted",
        raw={"verdict": "refuted"},
    )
    return AssertionContext(
        snapshot=snapshot((refuted,)), served_items=("hyp-prefetch",)
    )


_JOURNEY = (
    ("goal-1", "goal", "s1", ()),
    ("hyp-1", "hypothesis", "s1", ("goal-1",)),
    ("pred-1", "prediction", "s1", ("hyp-1",)),
    ("act-1", "intervention", "s2", ("pred-1",)),
    ("rec-1", "records", "s2", ("act-1",)),
    ("rev-1", "review", "s3", ("rec-1",)),
    ("hyp-2", "revision", "s3", ("hyp-1",)),
)


def _journey_items(stages: tuple[tuple[str, str, str, tuple[str, ...]], ...]):
    return tuple(
        item(
            item_id,
            kind="claim",
            cites=cites,
            raw={"stage": stage, "session": session},
        )
        for item_id, stage, session, cites in stages
    )


def loop_journey_state_coherent_pass() -> AssertionContext:
    items = _journey_items(_JOURNEY)
    return AssertionContext(
        snapshot=snapshot(items), prior=snapshot(items, phase="p0")
    )


def loop_journey_state_coherent_fail() -> AssertionContext:
    """The restart ate the revision the whole loop existed to produce."""

    items = _journey_items(_JOURNEY)
    return AssertionContext(
        snapshot=snapshot(items[:-1]), prior=snapshot(items, phase="p0")
    )


Factory = Callable[[], AssertionContext]

# --------------------------------------------------------------------------
# Amendment sequence 2 (no-nudge, f20-f26).
#
# Each fail fixture removes exactly one mechanism from its pass fixture — the
# vacuity hunt the house discipline requires. Where a fail fixture merely
# *added* something, the assertion would be passing for the wrong reason.
# --------------------------------------------------------------------------


def surface(name: str, projection: str = "complete") -> StateItem:
    """A surface marker: the queue was projected, and how completely."""

    return item(
        f"surface-{name}",
        kind="container",
        raw={"surface": name, "projection": projection},
    )


def all_surfaces(projection: str = "complete") -> tuple[StateItem, ...]:
    return tuple(
        surface(name, projection)
        for name in (
            "audit_findings",
            "review_queue",
            "proposal_queue",
            "due_state_counters",
        )
    )


def signal(
    signal_id: str,
    *,
    signal_class: str,
    targets: str,
    on: str = "review_queue",
    review_state: str | None = "open",
    extra: dict[str, str] | None = None,
) -> StateItem:
    raw = {"signal_class": signal_class, "targets": targets, "surface": on}
    raw.update(extra or {})
    return item(
        signal_id,
        kind="container",
        review_state=review_state,
        raw=raw,
    )


def signal_absence_checked_across_all_surfaces_pass() -> AssertionContext:
    twin = item("note-weekly-log", raw={"cluster_count": "5"})
    return AssertionContext(
        snapshot=snapshot((twin, *all_surfaces())), subject="note-weekly-log"
    )


def signal_absence_checked_across_all_surfaces_fail() -> AssertionContext:
    """The relocated nag: no queue item, but the counters block names the twin.

    Mechanism removed: nothing. Mechanism *moved* — which is exactly the cheat
    the meta-predicate exists to catch, so the quiet assertion must still fail.
    """

    twin = item("note-weekly-log", raw={"cluster_count": "5"})
    relocated = signal(
        "counter-entry",
        signal_class="promotion",
        targets="note-weekly-log",
        on="due_state_counters",
    )
    return AssertionContext(
        snapshot=snapshot((twin, relocated, *all_surfaces())), subject="note-weekly-log"
    )


def structural_signal_surfaced_within_budget_pass() -> AssertionContext:
    note = item("note-accumulating", raw={"cluster_count": "3"})
    proposal = signal(
        "sig-split", signal_class="promotion", targets="note-accumulating", on="proposal_queue"
    )
    return AssertionContext(
        snapshot=snapshot((note, proposal, *all_surfaces())), subject="note-accumulating"
    )


def structural_signal_surfaced_within_budget_fail() -> AssertionContext:
    """Mechanism removed: the detector emits no signal. Everything else identical."""

    note = item("note-accumulating", raw={"cluster_count": "3"})
    return AssertionContext(
        snapshot=snapshot((note, *all_surfaces())), subject="note-accumulating"
    )


def entity_candidate_surfaced_from_recurrence_pass() -> AssertionContext:
    identity = item("entity-kaupunki", raw={"source_count": "3"})
    candidate = signal(
        "sig-entity", signal_class="entity_candidate", targets="entity-kaupunki"
    )
    return AssertionContext(
        snapshot=snapshot((identity, candidate, *all_surfaces())), subject="entity-kaupunki"
    )


def entity_candidate_surfaced_from_recurrence_fail() -> AssertionContext:
    """Mechanism removed: the recurrence sensor emits nothing."""

    identity = item("entity-kaupunki", raw={"source_count": "3"})
    return AssertionContext(
        snapshot=snapshot((identity, *all_surfaces())), subject="entity-kaupunki"
    )


def contradiction_surfaced_unprompted_pass() -> AssertionContext:
    conclusion = item("claim-budget", kind="claim")
    evidence = item("ev-measurement", kind="evidence")
    pair = signal(
        "sig-conflict",
        signal_class="contradiction",
        targets="claim-budget,ev-measurement",
    )
    return AssertionContext(
        snapshot=snapshot((conclusion, evidence, pair, *all_surfaces())),
        subject="claim-budget",
        counterpart="ev-measurement",
    )


def contradiction_surfaced_unprompted_fail() -> AssertionContext:
    """Mechanism removed: the signal names only one side, so no pair surfaced."""

    conclusion = item("claim-budget", kind="claim")
    evidence = item("ev-measurement", kind="evidence")
    half = signal("sig-conflict", signal_class="contradiction", targets="claim-budget")
    return AssertionContext(
        snapshot=snapshot((conclusion, evidence, half, *all_surfaces())),
        subject="claim-budget",
        counterpart="ev-measurement",
    )


def _dismissal_prior() -> EpistemicStateSnapshot:
    dismissed = item(
        "note-standalone",
        review_state="dismissed",
        raw={"fingerprint": "fp-abc123", "passes": "0"},
    )
    return snapshot((dismissed, *all_surfaces()), phase="p1")


def dismissal_respected_across_passes_pass() -> AssertionContext:
    survived = item(
        "note-standalone",
        review_state="dismissed",
        raw={"fingerprint": "fp-abc123", "passes": "4"},
    )
    return AssertionContext(
        snapshot=snapshot((survived, *all_surfaces()), phase="p2"),
        prior=_dismissal_prior(),
        subject="note-standalone",
    )


def dismissal_respected_across_passes_fail() -> AssertionContext:
    """Mechanism removed: the triage store stops binding, so the same fingerprint returns."""

    survived = item(
        "note-standalone",
        review_state="dismissed",
        raw={"fingerprint": "fp-abc123", "passes": "4"},
    )
    resurfaced = signal(
        "sig-again",
        signal_class="promotion",
        targets="note-standalone",
        extra={"fingerprint": "fp-abc123"},
    )
    return AssertionContext(
        snapshot=snapshot((survived, resurfaced, *all_surfaces()), phase="p2"),
        prior=_dismissal_prior(),
        subject="note-standalone",
    )


def _counters_snapshot(phase: str, **raw: str):
    block = item(
        "surface-due_state_counters",
        kind="container",
        raw={"surface": "due_state_counters", "projection": "complete", **raw},
    )
    return snapshot((block,), phase=phase)


def _counters(*, before: dict[str, str] | None = None, **raw: str) -> AssertionContext:
    """A counters context. `before` seeds the PRIOR snapshot's cumulative totals."""
    return AssertionContext(
        snapshot=_counters_snapshot("p2", **raw),
        prior=None if before is None else _counters_snapshot("p1", **before),
    )


def counter_emission_not_repeated_per_write_pass() -> AssertionContext:
    return _counters(emissions="1", writes="12", due_total="4")


def counter_emission_not_repeated_per_write_fail() -> AssertionContext:
    """Mechanism removed: batching is gone, so one block is emitted per write."""

    return _counters(emissions="12", writes="12", due_total="4")


def test_the_counter_verdict_is_the_delta_between_the_snapshots() -> None:
    """Cumulative totals answer a question about the vault, not about the batch.

    `writes` and `emissions` count everything the vault ever did, so a ratio
    read off the later snapshot alone is a verdict on its whole history. The
    batch under test is `later - prior`, and both snapshots are already in the
    context.

    The row that matters is the third one. `due_total` — the size of the last
    delivered block — persists, so once ANY earlier delivery had happened it
    stayed positive forever, and a later batch that delivered nothing scored a
    clean `pass` on the strength of a block emitted before it began.
    """

    def outcome(**kwargs) -> str:
        return resolve("counter_emission_not_repeated_per_write")(
            _counters(**kwargs)
        ).outcome

    # One prior delivery, then a twelve-write batch that delivers NOTHING.
    assert (
        outcome(
            before={"emissions": "1", "writes": "3", "due_total": "4"},
            emissions="1",
            writes="15",
            due_total="4",
        )
        == "unsupported"
    )
    # The same history, but this batch did deliver once: decidable, and clean.
    assert (
        outcome(
            before={"emissions": "1", "writes": "3", "due_total": "4"},
            emissions="2",
            writes="15",
            due_total="4",
        )
        == "pass"
    )
    # ...and one per write inside the window is still a failure.
    assert (
        outcome(
            before={"emissions": "1", "writes": "3", "due_total": "4"},
            emissions="13",
            writes="15",
            due_total="4",
        )
        == "fail"
    )
    # No batch in the window at all.
    assert (
        outcome(
            before={"emissions": "1", "writes": "15", "due_total": "4"},
            emissions="1",
            writes="15",
        )
        == "unsupported"
    )
    # A missing prior is measured from zero, which is right for a fresh vault.
    assert outcome(emissions="1", writes="12") == "pass"
    assert outcome(emissions="0", writes="12") == "unsupported"
    # Counters that went backwards do not describe one batch.
    assert (
        outcome(before={"emissions": "5", "writes": "20"}, emissions="1", writes="12")
        == "unsupported"
    )


def test_due_total_no_longer_decides_the_counter_verdict() -> None:
    """It stays in the projection, informational, and gates nothing.

    Pinned because removing a gate quietly is how the next reviewer ends up
    re-deriving why it left. A zero `due_total` on a batch that DID deliver is
    no longer an `unsupported`, and a positive one on a batch that delivered
    nothing is no longer a `pass`.
    """

    def outcome(**kwargs) -> str:
        return resolve("counter_emission_not_repeated_per_write")(
            _counters(**kwargs)
        ).outcome

    assert outcome(emissions="1", writes="12", due_total="0") == "pass"
    assert (
        outcome(
            before={"emissions": "1", "writes": "3", "due_total": "9"},
            emissions="1",
            writes="15",
            due_total="9",
        )
        == "unsupported"
    )


def _packet_units() -> tuple[StateItem, ...]:
    return (
        item("dec-adopt-tunnel", kind="decision"),
        item("oq-latency-budget", kind="open_question"),
        item("plan-alpha", raw={"plan": "plan-alpha"}),
        item("decoy-foreign", kind="decision", raw={"decoy": "yes"}),
    )


def continuation_packet_reconstructs_session_pass() -> AssertionContext:
    packet = item(
        "packet-session-7",
        kind="container",
        cites=("dec-adopt-tunnel", "oq-latency-budget", "plan-alpha"),
        raw={"packet": "packet-session-7"},
    )
    return AssertionContext(snapshot=snapshot((packet, *_packet_units())))


def continuation_packet_reconstructs_session_fail() -> AssertionContext:
    """Mechanism removed: the packet drops a seeded unit it must hold by reference."""

    packet = item(
        "packet-session-7",
        kind="container",
        cites=("dec-adopt-tunnel", "plan-alpha"),
        raw={"packet": "packet-session-7"},
    )
    return AssertionContext(snapshot=snapshot((packet, *_packet_units())))


def restructure_signal_cleared_by_state_change_pass() -> AssertionContext:
    parent = item("note-restructured")
    children = (
        item("note-child-a", raw={"restructure_child": "note-restructured"}),
        item("note-child-b", raw={"restructure_child": "note-restructured"}),
    )
    return AssertionContext(
        snapshot=snapshot((parent, *children, *all_surfaces())), subject="note-restructured"
    )


def restructure_signal_cleared_by_state_change_fail() -> AssertionContext:
    """Mechanism removed: the lifecycle churns, proposing the new children back together."""

    parent = item("note-restructured")
    children = (
        item("note-child-a", raw={"restructure_child": "note-restructured"}),
        item("note-child-b", raw={"restructure_child": "note-restructured"}),
    )
    churn = signal(
        "sig-merge-back",
        signal_class="merge",
        targets="note-child-a",
        extra={"passes": "1"},
    )
    return AssertionContext(
        snapshot=snapshot((parent, *children, churn, *all_surfaces())),
        subject="note-restructured",
    )


def due_state_block_present_in_carrier_pass() -> AssertionContext:
    response = item(
        "resp-capture",
        kind="container",
        raw={"response_detail": "compact", "targets": "due_state_counters"},
    )
    return AssertionContext(snapshot=snapshot((response, *all_surfaces())))


def due_state_block_present_in_carrier_fail() -> AssertionContext:
    """Mechanism removed: the carrier drops the block from the compact response."""

    response = item("resp-capture", kind="container", raw={"response_detail": "compact"})
    return AssertionContext(snapshot=snapshot((response, *all_surfaces())))


DISCRIMINATION: dict[str, tuple[Factory, Factory]] = {
    "exactly_one_current_revision": (
        exactly_one_current_revision_pass,
        exactly_one_current_revision_fail,
    ),
    "no_retired_state_served_as_current": (
        no_retired_state_served_as_current_pass,
        no_retired_state_served_as_current_fail,
    ),
    "prior_revision_retained": (prior_revision_retained_pass, prior_revision_retained_fail),
    "revision_links_to_predecessor": (
        revision_links_to_predecessor_pass,
        revision_links_to_predecessor_fail,
    ),
    "evidence_path_exists": (evidence_path_exists_pass, evidence_path_exists_fail),
    "evidence_path_resolves": (evidence_path_resolves_pass, evidence_path_resolves_fail),
    "contradiction_visible": (contradiction_visible_pass, contradiction_visible_fail),
    "contradiction_not_flattened": (
        contradiction_not_flattened_pass,
        contradiction_not_flattened_fail,
    ),
    "decision_distinguishable_from_hypothesis": (
        decision_distinguishable_from_hypothesis_pass,
        decision_distinguishable_from_hypothesis_fail,
    ),
    "open_question_queryable": (open_question_queryable_pass, open_question_queryable_fail),
    "uncertainty_declared": (uncertainty_declared_pass, uncertainty_declared_fail),
    "review_state_durable": (review_state_durable_pass, review_state_durable_fail),
    "review_reopens_on_material_change": (
        review_reopens_on_material_change_pass,
        review_reopens_on_material_change_fail,
    ),
    "review_stays_closed_on_irrelevant_change": (
        review_stays_closed_on_irrelevant_change_pass,
        review_stays_closed_on_irrelevant_change_fail,
    ),
    "external_edit_authoritative_within": (
        external_edit_authoritative_within_pass,
        external_edit_authoritative_within_fail,
    ),
    "export_reconstructs_state": (export_reconstructs_state_pass, export_reconstructs_state_fail),
    "dependent_conclusions_surfaced_for_review": (
        dependent_conclusions_surfaced_for_review_pass,
        dependent_conclusions_surfaced_for_review_fail,
    ),
    "no_cross_case_residue": (no_cross_case_residue_pass, no_cross_case_residue_fail),
    "due_prediction_surfaced": (due_prediction_surfaced_pass, due_prediction_surfaced_fail),
    "verdict_state_retrievable": (
        verdict_state_retrievable_pass,
        verdict_state_retrievable_fail,
    ),
    "divergence_surfaced_without_mutation": (
        divergence_surfaced_without_mutation_pass,
        divergence_surfaced_without_mutation_fail,
    ),
    "support_collapse_inspectable": (
        support_collapse_inspectable_pass,
        support_collapse_inspectable_fail,
    ),
    "refuted_retrievable_at_full_standing": (
        refuted_retrievable_at_full_standing_pass,
        refuted_retrievable_at_full_standing_fail,
    ),
    "loop_journey_state_coherent": (
        loop_journey_state_coherent_pass,
        loop_journey_state_coherent_fail,
    ),
    "signal_absence_checked_across_all_surfaces": (
        signal_absence_checked_across_all_surfaces_pass,
        signal_absence_checked_across_all_surfaces_fail,
    ),
    "structural_signal_surfaced_within_budget": (
        structural_signal_surfaced_within_budget_pass,
        structural_signal_surfaced_within_budget_fail,
    ),
    "entity_candidate_surfaced_from_recurrence": (
        entity_candidate_surfaced_from_recurrence_pass,
        entity_candidate_surfaced_from_recurrence_fail,
    ),
    "contradiction_surfaced_unprompted": (
        contradiction_surfaced_unprompted_pass,
        contradiction_surfaced_unprompted_fail,
    ),
    "dismissal_respected_across_passes": (
        dismissal_respected_across_passes_pass,
        dismissal_respected_across_passes_fail,
    ),
    "counter_emission_not_repeated_per_write": (
        counter_emission_not_repeated_per_write_pass,
        counter_emission_not_repeated_per_write_fail,
    ),
    "continuation_packet_reconstructs_session": (
        continuation_packet_reconstructs_session_pass,
        continuation_packet_reconstructs_session_fail,
    ),
    "restructure_signal_cleared_by_state_change": (
        restructure_signal_cleared_by_state_change_pass,
        restructure_signal_cleared_by_state_change_fail,
    ),
    "due_state_block_present_in_carrier": (
        due_state_block_present_in_carrier_pass,
        due_state_block_present_in_carrier_fail,
    ),
}


def test_every_registered_assertion_has_a_discriminating_pair() -> None:
    assert set(DISCRIMINATION) == set(PREREGISTERED_ASSERTIONS)


@pytest.mark.parametrize("name", sorted(DISCRIMINATION))
def test_assertion_passes_its_passing_fixture(name: str) -> None:
    result = resolve(name)(DISCRIMINATION[name][0]())
    assert isinstance(result, AssertionResult)
    assert result.name == name
    assert result.outcome == "pass", result.evidence
    assert result.evidence


@pytest.mark.parametrize("name", sorted(DISCRIMINATION))
def test_assertion_fails_its_failing_fixture(name: str) -> None:
    result = resolve(name)(DISCRIMINATION[name][1]())
    assert result.name == name
    assert result.outcome == "fail", result.evidence
    assert result.evidence


@pytest.mark.parametrize("name", sorted(DISCRIMINATION))
def test_assertions_are_deterministic(name: str) -> None:
    passing, failing = DISCRIMINATION[name]
    fn = ASSERTION_REGISTRY[name]
    assert fn(passing()) == fn(passing())
    assert fn(failing()) == fn(failing())


# --------------------------------------------------------------------------
# Five-valued capability honesty.
# --------------------------------------------------------------------------


def test_absent_by_design_declaration_yields_not_applicable() -> None:
    ctx = uncertainty_declared_fail()
    ctx = ctx.replace(
        snapshot=snapshot(
            ctx.snapshot.items, decls=declarations(uncertainty="absent_by_design")
        ),
        subject="claim-1",
    )
    result = resolve("uncertainty_declared")(ctx)
    assert result.outcome == "not_applicable"


def test_unavailable_declaration_yields_unsupported() -> None:
    ctx = uncertainty_declared_fail()
    ctx = ctx.replace(
        snapshot=snapshot(ctx.snapshot.items, decls=declarations(uncertainty="unavailable")),
        subject="claim-1",
    )
    result = resolve("uncertainty_declared")(ctx)
    assert result.outcome == "unsupported"


def test_missing_declaration_is_unsupported_never_a_zero() -> None:
    ctx = uncertainty_declared_fail()
    ctx = ctx.replace(
        snapshot=snapshot(ctx.snapshot.items, decls=()),
        subject="claim-1",
    )
    result = resolve("uncertainty_declared")(ctx)
    assert result.outcome == "unsupported"


def test_claim_conditioned_absence_scores_fail_with_the_claim_cited() -> None:
    """PREREGISTRATION §4: a product that claims the property cannot take N/A."""

    decls = tuple(
        FieldDeclaration(
            field=declaration.field,
            status="absent_by_design",
            evidence=declaration.evidence,
            marketing_claim="the product page advertises per-conclusion uncertainty",
        )
        if declaration.field == "uncertainty"
        else declaration
        for declaration in declarations()
    )
    ctx = uncertainty_declared_fail()
    ctx = ctx.replace(
        snapshot=snapshot(ctx.snapshot.items, decls=decls), subject="claim-1"
    )
    result = resolve("uncertainty_declared")(ctx)
    assert result.outcome == "fail"
    assert "advertises per-conclusion uncertainty" in result.evidence


# --------------------------------------------------------------------------
# Acceptance predicates: >=2 structurally different representations pass.
# --------------------------------------------------------------------------


def test_prior_revision_retained_accepts_three_representations() -> None:
    fn = resolve("prior_revision_retained")

    chain = _revision_pair(both_current=False).replace(subject="budget-v2")
    assert fn(chain).outcome == "pass"

    # The scenario names the predecessor it planted; the product retains the
    # superseded artifact without exposing a link. That is representation #2,
    # and it is scoped to the declared predecessor rather than to any retired
    # item anywhere in the snapshot (see B1).
    retained_only = AssertionContext(
        snapshot=snapshot(
            (
                item("budget-v1", current="no", retired_reason="superseded"),
                item("budget-v2", current="yes"),
            )
        ),
        subject="budget-v2",
        counterpart="budget-v1",
    )
    assert fn(retained_only).outcome == "pass"

    declared_mechanism = AssertionContext(
        snapshot=snapshot(
            (item("budget-v2", current="yes"),),
            decls=declarations(prior_revision="available_via:vcs"),
        ),
        subject="budget-v2",
    )
    passed = fn(declared_mechanism)
    assert passed.outcome == "pass"
    assert "vcs" in passed.evidence

    assert len({fn(chain).evidence, fn(retained_only).evidence, passed.evidence}) == 3


def test_revision_links_to_predecessor_accepts_three_representations() -> None:
    fn = resolve("revision_links_to_predecessor")

    explicit_edge = _revision_pair(both_current=False).replace(subject="budget-v2")
    assert fn(explicit_edge).outcome == "pass"

    in_content = AssertionContext(
        snapshot=snapshot(
            (
                item("budget-v1", title="retrieval-budget-v1", current="no"),
                item(
                    "budget-v2",
                    title="retrieval-budget-v2",
                    current="yes",
                    text="This restatement replaces retrieval-budget-v1 after new evidence.",
                ),
            )
        ),
        subject="budget-v2",
        counterpart="budget-v1",
    )
    assert fn(in_content).outcome == "pass"

    version_chain = AssertionContext(
        snapshot=snapshot(
            (
                item("budget-v1", current="no", revision_chain_id="chain-b", revision_index=0),
                item("budget-v2", current="yes", revision_chain_id="chain-b", revision_index=1),
            )
        ),
        subject="budget-v2",
    )
    assert fn(version_chain).outcome == "pass"


def test_contradiction_visible_accepts_three_representations() -> None:
    fn = resolve("contradiction_visible")

    typed_edge = contradiction_visible_pass()
    assert fn(typed_edge).outcome == "pass"

    both_current_with_marker = AssertionContext(
        snapshot=snapshot(
            (
                item("claim-a", current="yes", review_state="conflict"),
                item("claim-b", current="yes", review_state="conflict"),
            )
        ),
        subject="claim-a",
        counterpart="claim-b",
    )
    assert fn(both_current_with_marker).outcome == "pass"

    review_queue_entry = AssertionContext(
        snapshot=snapshot(
            (
                item("claim-a", current="yes"),
                item("claim-b", current="yes"),
                item(
                    "queue-1",
                    kind="container",
                    current="yes",
                    review_state="conflict",
                    cites=("claim-a", "claim-b"),
                ),
            )
        ),
        subject="claim-a",
        counterpart="claim-b",
    )
    assert fn(review_queue_entry).outcome == "pass"


def test_decision_distinguishable_accepts_three_representations() -> None:
    fn = resolve("decision_distinguishable_from_hypothesis")

    assert fn(decision_distinguishable_from_hypothesis_pass()).outcome == "pass"

    folder_convention = AssertionContext(
        snapshot=snapshot(
            (
                item(
                    "decide-1",
                    kind="claim",
                    current="yes",
                    locator="Notes/Decisions/pick-bounded-retrieval",
                    locator_kind="file",
                ),
                item(
                    "hypo-1",
                    kind="claim",
                    current="yes",
                    locator="Notes/Hypotheses/maybe-bounded-retrieval",
                    locator_kind="file",
                ),
            )
        ),
        subject="decide-1",
        counterpart="hypo-1",
    )
    assert fn(folder_convention).outcome == "pass"

    metadata_attribute = AssertionContext(
        snapshot=snapshot(
            (
                item("decide-1", kind="claim", current="yes", raw={"record_type": "decision"}),
                item("hypo-1", kind="claim", current="yes", raw={"record_type": "hypothesis"}),
            )
        ),
        subject="decide-1",
        counterpart="hypo-1",
    )
    assert fn(metadata_attribute).outcome == "pass"


def test_open_question_queryable_accepts_three_representations() -> None:
    fn = resolve("open_question_queryable")

    assert fn(open_question_queryable_pass()).outcome == "pass"

    tagged = AssertionContext(
        snapshot=snapshot((item("claim-1", current="yes", raw={"tags": "open-question"}),))
    )
    assert fn(tagged).outcome == "pass"

    queue_entry = AssertionContext(
        snapshot=snapshot(
            (item("queue-1", kind="container", current="yes", review_state="open"),)
        )
    )
    assert fn(queue_entry).outcome == "pass"


def test_evidence_path_resolves_accepts_frontmatter_and_typed_relations() -> None:
    fn = resolve("evidence_path_resolves")

    assert fn(evidence_path_resolves_pass()).outcome == "pass"

    via_relations = AssertionContext(
        snapshot=snapshot(
            (
                item("src-1", kind="raw_source", current="yes"),
                item("claim-1", current="yes"),
            ),
            relations=(
                Relation(subject="claim-1", predicate="evidenced_by", object="src-1"),
            ),
        )
    )
    assert fn(via_relations).outcome == "pass"


def test_export_reconstructs_state_honours_tolerance() -> None:
    fn = resolve("export_reconstructs_state")
    lossy = _export_pair(complete=False)
    assert fn(lossy).outcome == "fail"
    assert fn(lossy.replace(tolerance=0.75)).outcome == "pass"


def test_external_edit_outside_the_bound_fails() -> None:
    fn = resolve("external_edit_authoritative_within")
    late = _external_edit(adopted=True).replace(freshness_bound_s=5.0)
    result = fn(late)
    assert result.outcome == "fail"
    assert "5.0" in result.evidence


def test_external_edit_without_a_declared_bound_is_unsupported() -> None:
    fn = resolve("external_edit_authoritative_within")
    result = fn(_external_edit(adopted=True).replace(freshness_bound_s=None))
    assert result.outcome == "unsupported"


def test_no_cross_case_residue_without_a_canary_probe_is_unsupported() -> None:
    fn = resolve("no_cross_case_residue")
    ctx = AssertionContext(snapshot=snapshot((item("claim-1", current="yes"),)))
    assert fn(ctx).outcome == "unsupported"


# ==========================================================================
# Correction round — adversarial fixtures from the independent review.
# Each test below reproduces the reviewer's exact scenario for one finding.
# ==========================================================================


def test_b1_unrelated_retired_item_is_not_history_retention() -> None:
    """B1: an unrelated retired note must not satisfy a catastrophic invariant."""

    fn = resolve("prior_revision_retained")
    ctx = AssertionContext(
        snapshot=snapshot(
            (
                item("budget-v2", current="yes"),
                item(
                    "unrelated-old-note",
                    current="no",
                    retired_reason="archived; different lineage entirely",
                ),
            )
        ),
        subject="budget-v2",
    )
    result = fn(ctx)
    assert result.outcome == "fail", result.evidence


def test_b1_dangling_revision_of_is_a_failure_not_a_fall_through() -> None:
    """B1: a declared predecessor that no longer resolves is destroyed history."""

    fn = resolve("prior_revision_retained")
    ctx = AssertionContext(
        snapshot=snapshot(
            (
                item("budget-v2", current="yes", revision_of="budget-v1"),
                item("unrelated-old-note", current="no", retired_reason="archived"),
            )
        ),
        subject="budget-v2",
    )
    result = fn(ctx)
    assert result.outcome == "fail", result.evidence
    assert "budget-v1" in result.evidence


def test_m1_absent_by_design_primary_survives_an_observable_sibling_external_edit() -> None:
    """M1: a declared sibling must not turn a designed absence into a fail."""

    fn = resolve("external_edit_authoritative_within")
    ctx = _external_edit(adopted=False)
    ctx = ctx.replace(
        snapshot=snapshot(
            ctx.snapshot.items,
            decls=declarations(external_edit="absent_by_design"),
            phase="post",
            taken_at=ctx.snapshot.taken_at,
        )
    )
    result = fn(ctx)
    assert result.outcome == "not_applicable", result.evidence
    assert "external_edit" in result.evidence


def test_m1_absent_by_design_primary_survives_an_observable_sibling_open_question() -> None:
    """M1: ``kind`` being declared must not override ``open_question`` absence."""

    fn = resolve("open_question_queryable")
    ctx = open_question_queryable_fail()
    ctx = ctx.replace(
        snapshot=snapshot(
            ctx.snapshot.items, decls=declarations(open_question="absent_by_design")
        )
    )
    result = fn(ctx)
    assert result.outcome == "not_applicable", result.evidence
    assert "open_question" in result.evidence


def test_m2_silent_rewrite_of_a_dependent_is_a_failure() -> None:
    """M2: rewriting a dependent instead of surfacing it is the harm, not the cure."""

    fn = resolve("dependent_conclusions_surfaced_for_review")
    before = snapshot(
        (
            item("src-1", kind="raw_source", current="yes"),
            item("claim-1", current="yes", cites=("src-1",)),
        ),
        phase="pre",
    )
    after = snapshot(
        (
            item(
                "src-1",
                kind="raw_source",
                current="no",
                retired_reason="superseded by a corrected capture",
            ),
            item(
                "claim-1",
                current="yes",
                cites=("src-1",),
                text="quietly rewritten to match the new capture",
            ),
        ),
        phase="post",
        taken_at="2026-01-02T00:00:00Z",
    )
    result = fn(AssertionContext(snapshot=after, prior=before))
    assert result.outcome == "fail", result.evidence
    assert "claim-1" in result.evidence


def test_m4_metadata_attribute_must_state_decision_or_hypothesis() -> None:
    """M4: attributes that merely differ do not distinguish the two concepts."""

    fn = resolve("decision_distinguishable_from_hypothesis")
    ctx = AssertionContext(
        snapshot=snapshot(
            (
                item("decide-1", kind="claim", current="yes", raw={"status": "active"}),
                item("hypo-1", kind="claim", current="yes", raw={"status": "draft"}),
            )
        ),
        subject="decide-1",
        counterpart="hypo-1",
    )
    result = fn(ctx)
    assert result.outcome == "fail", result.evidence


def test_m5_in_content_reference_pool_is_scoped_to_the_lineage() -> None:
    """M5: naming some unrelated archived note is not naming your predecessor."""

    fn = resolve("revision_links_to_predecessor")
    ctx = AssertionContext(
        snapshot=snapshot(
            (
                item(
                    "budget-v2",
                    current="yes",
                    text="Supersedes nothing here, but it does mention stale-pricing-note.",
                ),
                item(
                    "stale-pricing-note",
                    title="stale-pricing-note",
                    current="no",
                    retired_reason="archived; unrelated topic",
                ),
            )
        ),
        subject="budget-v2",
    )
    result = fn(ctx)
    assert result.outcome == "fail", result.evidence


def test_m6_export_dropping_relation_edges_fails() -> None:
    """M6: reconstruction must cover typed relations, not just ``cites``."""

    fn = resolve("export_reconstructs_state")
    live_items = (
        item("src-1", kind="raw_source", current="yes"),
        item("claim-1", current="yes", cites=("src-1",)),
    )
    live = snapshot(
        live_items,
        relations=(
            Relation(subject="claim-1", predicate="cites", object="src-1"),
            Relation(subject="claim-1", predicate="derived_from", object="src-1"),
        ),
        phase="live",
    )
    derived = snapshot(live_items, relations=(), phase="export")
    result = fn(AssertionContext(snapshot=derived, prior=live, tolerance=0.0))
    assert result.outcome == "fail", result.evidence


def test_n1_two_chains_sharing_a_chain_id_are_both_evaluated() -> None:
    """N1: keying groups on a shared chain id silently dropped a violation."""

    fn = resolve("exactly_one_current_revision")
    ctx = AssertionContext(
        snapshot=snapshot(
            (
                # Violating chain first: under the old keying it was overwritten
                # by the clean chain that shares its declared chain id.
                item("b1", current="yes", revision_chain_id="shared", revision_index=0),
                item(
                    "b2",
                    current="yes",
                    revision_of="b1",
                    revision_chain_id="shared",
                    revision_index=1,
                ),
                item("a1", current="no", revision_chain_id="shared", revision_index=0),
                item(
                    "a2",
                    current="yes",
                    revision_of="a1",
                    revision_chain_id="shared",
                    revision_index=1,
                ),
            )
        )
    )
    result = fn(ctx)
    assert result.outcome == "fail", result.evidence


def test_n2_negated_review_state_is_not_a_conflict_marker() -> None:
    """N2: ``no conflict`` matched ``conflict`` under bare text matching."""

    fn = resolve("contradiction_visible")
    ctx = AssertionContext(
        snapshot=snapshot(
            (
                item("claim-a", current="yes", review_state="no conflict"),
                item("claim-b", current="yes", review_state="no conflict"),
            )
        ),
        subject="claim-a",
        counterpart="claim-b",
    )
    result = fn(ctx)
    assert result.outcome == "fail", result.evidence


def test_n6_undeclared_currency_on_a_superseded_item_is_flagged() -> None:
    """N6: leaving currency undeclared must not hide retired state."""

    fn = resolve("no_retired_state_served_as_current")
    ctx = AssertionContext(
        snapshot=snapshot(
            (
                item(
                    "budget-v1",
                    current="undeclared",
                    retired_reason="superseded by budget-v2",
                ),
                item("budget-v2", current="yes", revision_of="budget-v1"),
            )
        )
    )
    result = fn(ctx)
    assert result.outcome == "fail", result.evidence
    assert "budget-v1" in result.evidence


def test_residual1_arbitrary_differing_folders_do_not_distinguish() -> None:
    """Two items merely filed in different folders say nothing about decidedness."""

    fn = resolve("decision_distinguishable_from_hypothesis")
    ctx = AssertionContext(
        snapshot=snapshot(
            (
                item(
                    "decide-1",
                    kind="claim",
                    current="yes",
                    locator="Notes/Alpha/pick-bounded-retrieval",
                    locator_kind="file",
                ),
                item(
                    "hypo-1",
                    kind="claim",
                    current="yes",
                    locator="Notes/Beta/maybe-bounded-retrieval",
                    locator_kind="file",
                ),
            )
        ),
        subject="decide-1",
        counterpart="hypo-1",
    )
    result = fn(ctx)
    assert result.outcome == "fail", result.evidence


def test_residual1_collection_vocabulary_is_closed_and_case_insensitive() -> None:
    """The documented convention is a closed vocabulary, matched case-blind."""

    fn = resolve("decision_distinguishable_from_hypothesis")
    for decision_folder, hypothesis_folder in (
        ("Notes/Decisions", "Notes/Hypotheses"),
        ("kb/decision", "kb/hypothesis"),
        ("VAULT/DECISIONS", "VAULT/PROPOSALS"),
        ("notes/decision", "notes/proposal"),
    ):
        ctx = AssertionContext(
            snapshot=snapshot(
                (
                    item(
                        "decide-1",
                        kind="claim",
                        current="yes",
                        locator=f"{decision_folder}/settled",
                        locator_kind="file",
                    ),
                    item(
                        "hypo-1",
                        kind="claim",
                        current="yes",
                        locator=f"{hypothesis_folder}/unsettled",
                        locator_kind="file",
                    ),
                )
            ),
            subject="decide-1",
            counterpart="hypo-1",
        )
        result = fn(ctx)
        assert result.outcome == "pass", (decision_folder, hypothesis_folder, result.evidence)


def test_rb1b_declared_but_unresolvable_subject_is_unsupported_not_a_pass() -> None:
    """R-B1b: an unobservable subject must not inherit the snapshot-wide widening.

    Reachable for real: ``VaultProjector`` ids are vault-relative paths while
    scenario fixtures declare logical ids, so a mismatch produces a declared
    subject that resolves to nothing. Widening to every revision group in that
    case reinstates the original B1 free pass on a catastrophic assertion.
    """

    fn = resolve("prior_revision_retained")
    ctx = AssertionContext(
        snapshot=snapshot(
            (
                item("Knowledge Base/Notes/Research/retrieval-budget-v2", current="yes"),
                item(
                    "Knowledge Base/Archive/old-note",
                    current="no",
                    retired_reason="archived; unrelated to the budget lineage",
                ),
            )
        ),
        subject="claim-budget-v2",
    )
    result = fn(ctx)
    assert result.outcome != "pass", result.evidence
    assert result.outcome == "unsupported", result.evidence
    assert "claim-budget-v2" in result.evidence
    assert "not observable" in result.evidence


def test_rb1b_omitted_subject_keeps_the_snapshot_wide_reading() -> None:
    """R-B1b: pin the intended wide behaviour so the fix cannot over-tighten."""

    fn = resolve("prior_revision_retained")
    ctx = AssertionContext(
        snapshot=snapshot(
            (
                item("budget-v1", current="no", retired_reason="superseded"),
                item("budget-v2", current="yes"),
            )
        )
    )
    result = fn(ctx)
    assert result.outcome == "pass", result.evidence
    assert "budget-v1" in result.evidence


def test_sibling_guard_decision_unresolvable_declared_pair_is_unsupported() -> None:
    """A declared pair that is not in the projection must not fall back wide.

    Same shape as R-B1b: unrelated decision/hypothesis items elsewhere in the
    snapshot answered a question about two items nobody could observe.
    """

    fn = resolve("decision_distinguishable_from_hypothesis")
    ctx = AssertionContext(
        snapshot=snapshot(
            (
                item("real-a", kind="decision", current="yes"),
                item("real-b", kind="hypothesis", current="yes"),
            )
        ),
        subject="ghost",
        counterpart="phantom",
    )
    result = fn(ctx)
    assert result.outcome != "pass", result.evidence
    assert result.outcome == "unsupported", result.evidence
    assert "ghost" in result.evidence
    assert "not observable" in result.evidence
    assert "real-a" not in result.evidence


def test_sibling_guard_decision_unresolvable_counterpart_is_unsupported() -> None:
    """The counterpart is a co-equal pair member; it gets the same guard."""

    fn = resolve("decision_distinguishable_from_hypothesis")
    ctx = AssertionContext(
        snapshot=snapshot(
            (
                item("real-a", kind="decision", current="yes"),
                item("real-b", kind="hypothesis", current="yes"),
            )
        ),
        subject="real-a",
        counterpart="phantom",
    )
    result = fn(ctx)
    assert result.outcome == "unsupported", result.evidence
    assert "phantom" in result.evidence
    assert "counterpart" in result.evidence


def test_sibling_guard_decision_omitted_subject_keeps_the_wide_reading() -> None:
    """Pin: with nothing declared, the snapshot-wide scan is the intended path."""

    fn = resolve("decision_distinguishable_from_hypothesis")
    ctx = AssertionContext(
        snapshot=snapshot(
            (
                item("real-a", kind="decision", current="yes"),
                item("real-b", kind="hypothesis", current="yes"),
            )
        )
    )
    result = fn(ctx)
    assert result.outcome == "pass", result.evidence
    assert "real-a" in result.evidence


def test_sibling_guard_revision_links_unresolvable_subject_is_unsupported() -> None:
    """No wrong-subject substitution: the verdict must be about what was declared."""

    fn = resolve("revision_links_to_predecessor")
    ctx = AssertionContext(
        snapshot=snapshot(
            (
                item("other-v1", current="no", retired_reason="superseded"),
                item("other-v2", current="yes", revision_of="other-v1"),
            )
        ),
        subject="ghost",
    )
    result = fn(ctx)
    assert result.outcome != "pass", result.evidence
    assert result.outcome == "unsupported", result.evidence
    assert "ghost" in result.evidence
    assert "not observable" in result.evidence
    assert "other-v2" not in result.evidence
    assert result.subject == "ghost"


def test_sibling_guard_revision_links_omitted_subject_keeps_the_wide_reading() -> None:
    """Pin: with nothing declared, picking the observed successor stays correct."""

    fn = resolve("revision_links_to_predecessor")
    ctx = AssertionContext(
        snapshot=snapshot(
            (
                item("other-v1", current="no", retired_reason="superseded"),
                item("other-v2", current="yes", revision_of="other-v1"),
            )
        )
    )
    result = fn(ctx)
    assert result.outcome == "pass", result.evidence
    assert "other-v2" in result.evidence
