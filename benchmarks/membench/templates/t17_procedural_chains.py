"""T17 — procedural chain: ordered steps, a precondition, and a revision.

A four-step runbook is authored in an operating manual, then a revision memo
retires step 2 and replaces it with a new step, superseding the affected
step-order claims. Queries cover current-order recall, the post-revision
predecessor (pre-revision predecessor forbidden), the as-of pre-revision
order, precondition recall, and an abstention for a step that never existed.
"""

from __future__ import annotations

from membench.procedural import (
    author_procedure,
    current_order,
    distinct_nouns,
    expect_current_order,
    expect_post_revision_predecessor,
    revise_step,
)
from membench.templates.base import (
    BuildContext,
    Template,
    expect_abstain,
    expect_superseded_history,
    expect_value,
    register,
)

TEMPLATE_ID = "t17_procedural_chains"
FAMILY = "procedural"


def build(ctx: BuildContext) -> None:
    nouns = distinct_nouns(ctx.rng, 7)
    step_labels = [f"{noun} stage" for noun in nouns[:4]]
    replacement_label = f"{nouns[4]} stage"
    ghost_label = f"{nouns[5]} stage"
    precondition_label = f"{nouns[6]} clearance"
    name = f"{ctx.project()} deploy runbook"

    proc = author_procedure(
        ctx,
        week=1,
        name=name,
        labels=step_labels,
        predecessor_targets=[3],
        precondition_label=precondition_label,
    )
    revision = revise_step(
        ctx, proc, week=6, position=2, replacement_label=replacement_label
    )
    _, ordered_claims, retired = current_order(proc, revision)
    target_label = proc.labels[2]  # position 3: the step the revision re-orders

    ctx.query(
        "current_truth",
        f"List the steps of the {name} in order, as the procedure stands today.",
        knowledge_week=9,
        family=FAMILY,
        expect=expect_current_order(ordered_claims, retired=retired),
    )
    ctx.query(
        "current_truth",
        f"After the revision memo, which step must come directly before the "
        f"{target_label} in the {name}?",
        knowledge_week=9,
        family=FAMILY,
        expect=expect_post_revision_predecessor(
            revision.new_predecessor_claims[3],
            proc.predecessor_claims[3],
            revision.source,
        ),
    )
    ctx.query(
        "as_of",
        f"As of week 3, before any revision, which step came directly before "
        f"the {target_label} in the {name}?",
        knowledge_week=9,
        world_week=3,
        family=FAMILY,
        expect=expect_superseded_history(
            proc.predecessor_claims[3], revision.new_predecessor_claims[3]
        ),
    )
    ctx.query(
        "direct_recall",
        f"What must be in place before step 1 of the {name} can start?",
        knowledge_week=9,
        family=FAMILY,
        expect=expect_value(proc.precondition_claim),
    )
    ctx.query(
        "unanswerable",
        f"Which position in the {name} is held by the {ghost_label}?",
        knowledge_week=9,
        family=FAMILY,
        expect=expect_abstain(),
    )


register(
    Template(
        template_id=TEMPLATE_ID,
        family=FAMILY,
        summary=(
            "Ordered how-to chain: current order, post-revision predecessor, "
            "as-of pre-revision order, precondition, never-existed step"
        ),
        variants=4,
        build=build,
    )
)
