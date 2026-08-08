"""Scorer families: 3-tier corpus health and graph multi-hop facts.

Health tiers: (1) adapter-native audit counts vs planted expectations,
(2) STATE_EXPORT shingling/orphan analysis, (3) neither capability ->
every metric is honestly ``unsupported`` with a None measurement — NEVER a
fabricated zero.

Graph: t14 encodes multi-hop expectations through ``query_kind='multi_hop'``
plus a required claim whose ``derived_from`` chain names the hops (no
dedicated gate string); the scorer keys on exactly that encoding.
"""

from __future__ import annotations

from membench.adapters.base import Capability, StateExport, StateExportPage
from membench.clock import week_date
from membench.schema import (
    Ask,
    Assertion,
    ClaimRecord,
    ClaimStatus,
    ExpectedAnswer,
    ExpectedRecord,
    QueryRecord,
    SpanCause,
    SpanCauseKind,
    Stance,
    StatusSpan,
    TypedValue,
)
from membench.scoring import GateStatus, ScoringContext
from membench.scoring.answer_contract import AnswerRecord
from membench.scoring.graph import score_graph
from membench.scoring.health import HEALTH_METRICS, score_health

# ------------------------------------------------------------------ health

_LONG_A = (
    "the deployment pipeline promotes builds through staging rings before "
    "production traffic shifts gradually across regions with automated "
    "rollback triggers armed on error budgets and latency ceilings"
)
# Only the FINAL word differs: 1 of 20 8-word shingles changes per side
# (19 shared / 21 in the union), so Jaccard ~ 0.90 >= 0.8 — a detectable
# near-duplicate.
_LONG_A_VARIANT = _LONG_A.replace("latency ceilings", "latency limits")
_LONG_B = (
    "quarterly planning consolidates roadmap themes into visible milestones "
    "with owners and measurable exit criteria reviewed at the monthly "
    "steering sync before budget adjustments land"
)


def _tier2_export() -> StateExport:
    return StateExport(
        pages=(
            StateExportPage(path="notes/pipeline.md", text=_LONG_A),
            StateExportPage(path="notes/pipeline-copy.md", text=_LONG_A_VARIANT),
            StateExportPage(path="notes/planning.md", text=_LONG_B),
            StateExportPage(
                path="notes/hub.md",
                text=(
                    "weekly hub: see [[notes/pipeline]] and [[notes/pipeline-copy]] "
                    "and [[notes/planning]]"
                ),
            ),
            StateExportPage(
                path="notes/orphan.md",
                text=(
                    "an unreferenced page about vendor onboarding checklists and "
                    "travel policy exceptions for the field crews"
                ),
            ),
            StateExportPage(path="notes/linker.md", text="points at [[notes/hub]] only"),
        )
    )


def test_health_tier2_state_export_duplicates_and_orphans() -> None:
    report = score_health(
        frozenset({Capability.STATE_EXPORT}),
        expected_counts={"duplicates": 1, "orphans": 2},
        export=_tier2_export(),
    )
    assert report.tier == 2
    by_gate = {item.gate: item for item in report.items}
    assert by_gate["health_duplicates"].status is GateStatus.PASS
    assert by_gate["health_orphans"].status is GateStatus.PASS
    # Tier 2 cannot derive these; they must be unsupported, never zero.
    assert by_gate["health_contradictions"].status is GateStatus.UNSUPPORTED
    assert by_gate["health_stale"].status is GateStatus.UNSUPPORTED
    # Two near-duplicate pages (8-word shingles, Jaccard >= 0.8) -> one pair;
    # orphan.md and linker.md have no inbound wikilinks.
    assert report.measurements["duplicates"] == 1
    assert report.measurements["orphans"] == 2
    assert report.measurements["contradictions"] is None
    assert report.measurements["stale"] is None


def test_health_tier2_wrong_expectation_fails_with_evidence() -> None:
    report = score_health(
        frozenset({Capability.STATE_EXPORT}),
        expected_counts={"duplicates": 0},
        export=_tier2_export(),
    )
    item = next(i for i in report.items if i.gate == "health_duplicates")
    assert item.status is GateStatus.FAIL
    assert "expected 0" in (item.evidence or "")


def test_health_tier3_unsupported_never_zero() -> None:
    report = score_health(
        frozenset(),
        expected_counts={"duplicates": 1, "contradictions": 2, "orphans": 3, "stale": 4},
    )
    assert report.tier == 3
    assert {item.gate for item in report.items} == {
        f"health_{metric}" for metric in HEALTH_METRICS
    }
    for item in report.items:
        assert item.status is GateStatus.UNSUPPORTED
    for metric in HEALTH_METRICS:
        assert report.measurements[metric] is None, f"{metric} must be None, not 0"


def test_health_tier1_native_audit_counts_vs_planted() -> None:
    report = score_health(
        frozenset({Capability.NATIVE_HEALTH_AUDIT}),
        expected_counts={"duplicates": 2, "stale": 1},
        audit_counts={"duplicates": 2, "contradictions": 0, "orphans": 5, "stale": 0},
    )
    assert report.tier == 1
    by_gate = {item.gate: item for item in report.items}
    assert by_gate["health_duplicates"].status is GateStatus.PASS
    assert by_gate["health_stale"].status is GateStatus.FAIL
    assert "reported 0" in (by_gate["health_stale"].evidence or "")
    # Audit reported counts without a planted expectation are informational.
    assert by_gate["health_contradictions"].status is GateStatus.NOT_APPLICABLE
    assert by_gate["health_orphans"].status is GateStatus.NOT_APPLICABLE
    assert report.measurements["orphans"] == 5


# ------------------------------------------------------------------- graph


def _claim(claim_id: str, predicate: str, value: TypedValue, *, derived_from=()) -> ClaimRecord:
    return ClaimRecord(
        claim_id=claim_id,
        subject="ENT-X",
        predicate=predicate,
        object=value,
        assertions=[
            Assertion(
                source_id="SRC-GRAPH001",
                stance=Stance.SUPPORTS,
                asserted_at=week_date(3, 1),
                recorded_week=3,
            )
        ],
        status_timeline=[
            StatusSpan(
                status=ClaimStatus.CURRENT,
                valid_from=week_date(3, 0),
                recorded_week=3,
                cause=SpanCause(kind=SpanCauseKind.INITIAL, by="SRC-GRAPH001"),
            )
        ],
        derived_from=list(derived_from),
    )


def _graph_fixture() -> tuple[QueryRecord, ExpectedRecord, ScoringContext]:
    c_owns = _claim(
        "CLM-OWNS",
        "owning_project",
        TypedValue(kind="entity_ref", value="Project Blue"),
    )
    c_lead = _claim(
        "CLM-LEAD",
        "project_lead",
        TypedValue(kind="entity_ref", value="Dana Field"),
        derived_from=["CLM-OWNS"],
    )
    query = QueryRecord(
        query_id="QRY-G1",
        template_id="t14_identity_graph",
        family="identity",
        query_kind="multi_hop",
        prompt_text="Who leads the project that owns the component?",
        ask=Ask(knowledge_week=8),
    )
    expected = ExpectedRecord(
        query_id="QRY-G1",
        answer=ExpectedAnswer(kind="entity", values=["Dana Field"]),
        required_claims=["CLM-LEAD"],
        required_citations=["SRC-GRAPH001"],
        gates=["current_state", "citations"],
    )
    ctx = ScoringContext(
        claims_by_id={"CLM-OWNS": c_owns, "CLM-LEAD": c_lead}, sources_by_id={}
    )
    return query, expected, ctx


def test_graph_right_entity_chain_passes() -> None:
    query, expected, ctx = _graph_fixture()
    answer = AnswerRecord(
        query_id="QRY-G1",
        answer_text="Dana Field leads the owning project. [ref:SRC-GRAPH001]",
    )
    items = score_graph(query, expected, answer, ctx)
    assert len(items) == 1
    item = items[0]
    assert item.gate == "graph_multi_hop"
    assert item.dimension == "graph"
    assert item.status is GateStatus.PASS


def test_graph_wrong_hop_fails_with_hop_evidence() -> None:
    query, expected, ctx = _graph_fixture()
    answer = AnswerRecord(
        query_id="QRY-G1",
        answer_text="Project Blue is responsible for that component.",
    )
    items = score_graph(query, expected, answer, ctx)
    assert items[0].status is GateStatus.FAIL
    assert "wrong hop" in (items[0].evidence or "")
    assert "Project Blue" in (items[0].evidence or "")


def test_graph_endpoint_absent_fails() -> None:
    query, expected, ctx = _graph_fixture()
    answer = AnswerRecord(query_id="QRY-G1", answer_text="I do not know.")
    items = score_graph(query, expected, answer, ctx)
    assert items[0].status is GateStatus.FAIL
    assert "wrong hop" not in (items[0].evidence or "")


def test_graph_not_applicable_for_single_hop_queries() -> None:
    query, expected, ctx = _graph_fixture()
    single = query.model_copy(update={"query_kind": "current_truth"})
    answer = AnswerRecord(query_id="QRY-G1", answer_text="Dana Field")
    items = score_graph(single, expected, answer, ctx)
    assert items[0].status is GateStatus.NOT_APPLICABLE
