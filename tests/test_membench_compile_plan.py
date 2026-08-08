"""The compile plan: oracle-derived conclusions, before any generation wiring.

Track B bulk-loads raw sources and never compiles, so the structure `provenance`
and `contradiction_uncertainty` score does not exist — 205 sources, 0 compiled
notes, `ingested_into: []` on 204 of 204 (2026-08-07 vault). The plan is what
gives a compiled altitude something to build.

Every field is derived from records the oracle already holds. No model is
involved: this is transduction of known ground truth, not reasoning about what
deserves remembering. That limit is real — a scripted compile is more faithful
than a bulk dump and less faithful than an agent choosing what to keep.

Pure logic only. Generation wiring, native rendering and scoring come later
(tasks 1.3+), so nothing here touches corpus bytes.
"""

from __future__ import annotations

from datetime import date

import pytest

from membench.compile_plan import ConclusionRecord, conclusion_id_for, derive_compile_plan
from membench.schema import (
    Assertion,
    ClaimRecord,
    ClaimStatus,
    SpanCause,
    SpanCauseKind,
    Stance,
    StatusSpan,
    TypedValue,
)


def _claim(
    claim_id: str,
    subject: str,
    predicate: str,
    value: str,
    *,
    sources: tuple[str, ...] = ("SRC-AAAA0001",),
    disputing: tuple[str, ...] = (),
    status: ClaimStatus = ClaimStatus.CURRENT,
    supersedes: str | None = None,
    superseded_by: str | None = None,
    recorded_week: int = 1,
) -> ClaimRecord:
    assertions = [
        Assertion(
            source_id=s,
            stance=Stance.SUPPORTS,
            asserted_at=date(2025, 3, 14),
            recorded_week=recorded_week,
        )
        for s in sources
    ]
    assertions += [
        Assertion(
            source_id=s,
            stance=Stance.DISPUTES,
            asserted_at=date(2025, 3, 14),
            recorded_week=recorded_week,
        )
        for s in disputing
    ]
    return ClaimRecord(
        claim_id=claim_id,
        subject=subject,
        predicate=predicate,
        object=TypedValue(kind="quantity", value=value, unit="staff"),
        assertions=assertions,
        status_timeline=[
            StatusSpan(
                status=status,
                valid_from=date(2025, 3, 14),
                recorded_week=recorded_week,
                cause=SpanCause(kind=SpanCauseKind.INITIAL, by=sources[0] if sources else None),
            )
        ],
        supersedes=supersedes,
        superseded_by=superseded_by,
    )


# --------------------------------------------------------------------------
# Citation derivation
# --------------------------------------------------------------------------


def test_a_conclusion_cites_the_sources_that_support_its_claim() -> None:
    claims = [_claim("CLM-0001", "ENT-1", "headcount", "173", sources=("SRC-A", "SRC-B"))]
    plan = derive_compile_plan(claims)
    assert len(plan) == 1
    assert plan[0].cites == ("SRC-A", "SRC-B")


def test_a_disputing_source_is_not_cited_as_the_conclusion_s_basis() -> None:
    """A source that objects to a claim is evidence ABOUT it, not evidence FOR it.

    Citing objectors as basis would make every disputed conclusion claim support
    it never had, and would inflate the permitted set the precision gate checks
    against.
    """

    claims = [
        _claim("CLM-0001", "ENT-1", "headcount", "173", sources=("SRC-A",), disputing=("SRC-B",))
    ]
    assert derive_compile_plan(claims)[0].cites == ("SRC-A",)


# --------------------------------------------------------------------------
# Lineage
# --------------------------------------------------------------------------


def test_supersession_carries_to_the_conclusion() -> None:
    claims = [
        _claim("CLM-0001", "ENT-1", "deadline", "2025-03-14", superseded_by="CLM-0002"),
        _claim("CLM-0002", "ENT-1", "deadline", "2025-03-28", supersedes="CLM-0001"),
    ]
    by_id = {c.claim_id: c for c in derive_compile_plan(claims)}
    # Edges reference CONCLUSIONS, not claims: the plan is a graph of the
    # conclusions a vault holds, and an adapter rendering it never sees a claim.
    assert by_id["CLM-0002"].supersedes == conclusion_id_for("CLM-0001")
    assert by_id["CLM-0001"].supersedes is None


def test_a_superseding_pair_is_not_a_dispute() -> None:
    """The trap in this derivation.

    Superseded and disputing claims look identical on a naive read — same
    subject, same predicate, different values. Treating a supersession chain as
    a dispute would manufacture conflicts across the whole temporal family and
    make the contradiction dimension meaningless in the opposite direction from
    today's.
    """

    claims = [
        _claim("CLM-0001", "ENT-1", "deadline", "2025-03-14", superseded_by="CLM-0002"),
        _claim("CLM-0002", "ENT-1", "deadline", "2025-03-28", supersedes="CLM-0001"),
    ]
    assert all(not c.disputes for c in derive_compile_plan(claims))


def test_two_live_claims_on_one_predicate_dispute_each_other() -> None:
    """The t07 shape: a system-of-record figure and a tentative rumour."""

    claims = [
        _claim("CLM-0001", "ENT-1", "headcount", "173", sources=("SRC-A",)),
        _claim(
            "CLM-0002",
            "ENT-1",
            "headcount",
            "149",
            sources=("SRC-B",),
            status=ClaimStatus.TENTATIVE,
        ),
    ]
    by_id = {c.claim_id: c for c in derive_compile_plan(claims)}
    assert by_id["CLM-0001"].disputes == (conclusion_id_for("CLM-0002"),)
    assert by_id["CLM-0002"].disputes == (conclusion_id_for("CLM-0001"),)


def test_same_value_on_one_predicate_is_corroboration_not_dispute() -> None:
    claims = [
        _claim("CLM-0001", "ENT-1", "headcount", "173", sources=("SRC-A",)),
        _claim("CLM-0002", "ENT-1", "headcount", "173", sources=("SRC-B",)),
    ]
    assert all(not c.disputes for c in derive_compile_plan(claims))


def test_different_subjects_never_dispute() -> None:
    claims = [
        _claim("CLM-0001", "ENT-1", "headcount", "173"),
        _claim("CLM-0002", "ENT-2", "headcount", "149"),
    ]
    assert all(not c.disputes for c in derive_compile_plan(claims))


# --------------------------------------------------------------------------
# Contract properties
# --------------------------------------------------------------------------


def test_the_plan_is_deterministic_and_order_independent() -> None:
    """Corpus bytes depend on this, and the release manifest hashes it."""

    claims = [
        _claim("CLM-0002", "ENT-1", "headcount", "149", sources=("SRC-B",)),
        _claim("CLM-0001", "ENT-1", "headcount", "173", sources=("SRC-A",)),
    ]
    forward = derive_compile_plan(claims)
    reversed_input = derive_compile_plan(list(reversed(claims)))
    assert [c.model_dump() for c in forward] == [c.model_dump() for c in reversed_input]


def test_the_record_carries_no_product_specific_field() -> None:
    """Neutrality guard (spec task 1.6).

    A record shaped by one product's API hands that product a native fit and
    everyone else a translation layer — the 2026-08-05 renderer defect with the
    sign flipped. These names are all borrowed from real product APIs and must
    never appear here.
    """

    fields = set(ConclusionRecord.model_fields)
    forbidden = {
        "sources",  # exomem remember(sources=...)
        "ingested_into",
        "observations",  # basic-memory note grammar
        "relations",
        "permalink",
        "entity_type",
        "frontmatter",
    }
    assert not (fields & forbidden), f"product-specific field(s): {sorted(fields & forbidden)}"


def test_every_claim_becomes_exactly_one_conclusion() -> None:
    claims = [
        _claim("CLM-0001", "ENT-1", "headcount", "173"),
        _claim("CLM-0002", "ENT-2", "deadline", "2025-03-14"),
        _claim("CLM-0003", "ENT-3", "vendor", "Kelva"),
    ]
    plan = derive_compile_plan(claims)
    assert [c.claim_id for c in plan] == ["CLM-0001", "CLM-0002", "CLM-0003"]
    assert len({c.conclusion_id for c in plan}) == 3


def test_a_claim_with_no_supporting_source_is_refused() -> None:
    """A conclusion with no basis is exactly the 4b.13 defect — records whose
    citation precision is unverifiable, where a shotgun passes for free."""

    claims = [_claim("CLM-0001", "ENT-1", "headcount", "173", sources=(), disputing=("SRC-B",))]
    with pytest.raises(ValueError, match="no supporting source"):
        derive_compile_plan(claims)


# --------------------------------------------------------------------------
# Against the real corpus
# --------------------------------------------------------------------------


def test_derivation_finds_both_dispute_families_in_the_real_corpus() -> None:
    """Seed-1 carries two differently-shaped conflicts, and both must be found.

    - **t08 equal-authority**: both claims carry an explicit `disputed` status.
    - **t07 authority conflict**: NEITHER carries one. A system-of-record value
      sits at `current` and a rumour at `tentative`, and the disagreement exists
      only in the values.

    A derivation keyed on the `disputed` status would find the first family and
    silently miss the second, halving the contradiction dimension while looking
    correct. This is why the rule is value-disagreement-without-lineage rather
    than a status lookup.
    """

    from pathlib import Path

    from membench.schema import load_jsonl

    corpus = Path(__file__).resolve().parents[1] / "benchmarks/corpus/generated/s1"
    if not (corpus / "claims.jsonl").is_file():  # pragma: no cover - corpus optional
        pytest.skip("generated seed-1 corpus not present in this checkout")

    claims = load_jsonl(ClaimRecord, corpus / "claims.jsonl")
    plan = derive_compile_plan(claims)
    by_claim = {c.claim_id: c for c in claims}
    con_to_claim = {c.conclusion_id: c.claim_id for c in plan}

    pairs = {
        frozenset({c.claim_id, con_to_claim[d]}) for c in plan for d in c.disputes
    }
    assert pairs, "no dispute pair derived from the real corpus"

    def _marked(claim_id: str) -> bool:
        return any(
            span.status is ClaimStatus.DISPUTED
            for span in by_claim[claim_id].status_timeline
        )

    both = [p for p in pairs if all(_marked(c) for c in p)]
    neither = [p for p in pairs if not any(_marked(c) for c in p)]
    assert both, "no explicitly-disputed pair found (t08 family)"
    assert neither, "no implicit value-conflict pair found (t07 family)"
    # Every pair is one shape or the other; a half-marked pair would mean the
    # corpus and this derivation disagree about what a dispute is.
    assert len(both) + len(neither) == len(pairs)


def test_every_conclusion_in_the_real_corpus_has_a_basis() -> None:
    """The refusal must not fire on a real corpus — if it does, some template
    is emitting a claim no source supports (the 4b.13 class)."""

    from pathlib import Path

    from membench.schema import load_jsonl

    corpus = Path(__file__).resolve().parents[1] / "benchmarks/corpus/generated/s1"
    if not (corpus / "claims.jsonl").is_file():  # pragma: no cover - corpus optional
        pytest.skip("generated seed-1 corpus not present in this checkout")

    plan = derive_compile_plan(load_jsonl(ClaimRecord, corpus / "claims.jsonl"))
    assert all(c.cites for c in plan)
    assert len({c.conclusion_id for c in plan}) == len(plan)
