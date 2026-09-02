"""9.3: the adversarial packet a reviewer with no stake in the outcome is given.

The pre-registration binding is recomputed here from the real receipts, and the
suspicious-win flags are read off a cohort the real validator normalized, so
neither can be asserted into existence by the packet builder.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from epistemic.projectors.exomem_vault import VaultProjector
from epistemic.schema import load_scenario
from pydantic import ValidationError

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = REPO_ROOT / "benchmarks" / "epistemic" / "fixtures"
VAULT = FIXTURES / "vault"
CONTRACT = REPO_ROOT / "docs" / "benchmark-fairness-contract.md"

CHECKLIST_HEADING = "## Reviewer checklist (what to attack first)"

SCENARIO_ID = "scenario-1"
SCENARIO_SHA256 = "1" * 64


def _doc_reviewer_questions() -> tuple[str, ...]:
    """The reviewer checklist, unwrapped, straight out of the contract."""

    lines = CONTRACT.read_text(encoding="utf-8").splitlines()
    start = lines.index(CHECKLIST_HEADING)
    questions: list[str] = []
    current: str | None = None
    for line in lines[start + 1 :]:
        if line.startswith("## "):
            break
        if line.startswith("- "):
            if current is not None:
                questions.append(current)
            current = line[2:].strip()
        elif current is not None and line.startswith("  ") and line.strip():
            current = f"{current} {line.strip()}"
    if current is not None:
        questions.append(current)
    return tuple(questions)


def _replayable_cell(run_root: Path, *, provider: str, variant: str, outcome: str):
    from epistemic.assertions import AssertionContext, no_retired_state_served_as_current
    from epistemic.cohort import CohortAssertionResult, CohortExpectationIdentity
    from epistemic.evidence import persist_assertion_evidence
    from epistemic.snapshot import (
        EpistemicStateSnapshot,
        FieldDeclaration,
        ProjectorMeta,
        StateItem,
    )

    snapshot = EpistemicStateSnapshot(
        provider=provider,
        variant=variant,
        phase="p1",
        taken_at="2026-08-11T00:00:00Z",
        items=(
            StateItem(
                id="chain",
                kind="claim",
                title="chain",
                current="yes" if outcome == "fail" else "no",
                retired_reason="superseded",
            ),
        ),
        declarations=(
            FieldDeclaration(
                field="current", status="declared", evidence="https://example.invalid/current"
            ),
        ),
        projector=ProjectorMeta(
            name="fixture",
            version="1",
            author="test",
            endpoints_used=("broker:state.read",),
            loc=1,
        ),
    )
    context = AssertionContext(snapshot=snapshot, subject="chain")
    result = no_retired_state_served_as_current(context)
    assert result.outcome == outcome
    reference = persist_assertion_evidence(
        run_root=run_root,
        scenario_id=SCENARIO_ID,
        scenario_sha256=SCENARIO_SHA256,
        family_id="f01",
        phase_id="p1",
        expectation_ordinal=1,
        assertion=result.name,
        context=context,
        result=result,
    )
    return CohortAssertionResult(
        identity=CohortExpectationIdentity(
            scenario_id=SCENARIO_ID,
            scenario_sha256=SCENARIO_SHA256,
            phase_id="p1",
            expectation_ordinal=1,
            assertion=result.name,
            subject="chain",
        ),
        result=result,
        evidence_ref=reference,
    )


def _cohort(run_root: Path, *, product_outcome: str, control_outcome: str):
    from epistemic.cohort import EpistemicCohortRow, validate_epistemic_cohort

    rows = (
        EpistemicCohortRow(
            provider="exomem",
            variant="exomem-native",
            assertions=(
                _replayable_cell(
                    run_root, provider="exomem", variant="exomem-native", outcome=product_outcome
                ),
            ),
        ),
        EpistemicCohortRow(
            provider="grep-markdown",
            variant="grep-markdown",
            assertions=(
                _replayable_cell(
                    run_root,
                    provider="grep-markdown",
                    variant="grep-markdown",
                    outcome=control_outcome,
                ),
            ),
        ),
        EpistemicCohortRow(
            provider="no-memory",
            variant="no-memory",
            assertions=(
                _replayable_cell(
                    run_root, provider="no-memory", variant="no-memory", outcome=control_outcome
                ),
            ),
        ),
    )
    return validate_epistemic_cohort(run_id="run-1", rows=rows, run_root=run_root)


def _challenge_artifacts() -> dict[str, str]:
    from benchmarks.reports.adversarial import REVIEWER_CHALLENGES

    return {
        challenge.challenge_id: f"challenges/{challenge.challenge_id}.md"
        for challenge in REVIEWER_CHALLENGES
    }


def _packet(tmp_path: Path, *, product_outcome="pass", control_outcome="fail", **changes):
    from benchmarks.reports.adversarial import build_adversarial_packet

    arguments = {
        "run_id": "run-1",
        "repo_root": REPO_ROOT,
        "fairness": load_scenario(FIXTURES / "scenario-minimal.yaml").fairness,
        "declarations": VaultProjector(VAULT).declarations(),
        "cohort": _cohort(
            tmp_path, product_outcome=product_outcome, control_outcome=control_outcome
        ),
        "challenge_artifacts": _challenge_artifacts(),
    }
    arguments.update(changes)
    return build_adversarial_packet(**arguments)


def test_preregistration_binding_equals_the_recomputed_identity(tmp_path: Path) -> None:
    """R4: sha256 and amendment order are the real receipts', not the packet's."""

    from protocol.contracts import (
        derive_preregistration_identity,
        fold_amendment_chain,
        working_amendment_receipts,
    )

    packet = _packet(tmp_path)
    identity = derive_preregistration_identity(REPO_ROOT)
    assert packet.preregistration.sha256 == identity.effective.sha256
    assert packet.preregistration.base_sha256 == identity.original.sha256
    assert packet.preregistration.contract_revision == identity.contract_revision

    receipts = working_amendment_receipts(REPO_ROOT)
    assert receipts
    folded = fold_amendment_chain(
        receipts,
        base_sha256=packet.preregistration.base_sha256,
        current_sha256=packet.preregistration.sha256,
    )
    assert folded == packet.preregistration.sha256
    assert tuple(item.sequence for item in packet.preregistration.amendments) == tuple(
        receipt.sequence for receipt in receipts
    )
    assert tuple(item.contract_sha256 for item in packet.preregistration.amendments) == tuple(
        receipt.contract_sha256 for receipt in receipts
    )
    assert tuple(item.receipt_path for item in packet.preregistration.amendments) == tuple(
        amendment.receipt.receipt_path for amendment in identity.amendments
    )


def test_assumptions_and_confounds_come_from_declarations_and_the_packet(
    tmp_path: Path,
) -> None:
    scenario = load_scenario(FIXTURES / "scenario-minimal.yaml")
    packet = _packet(tmp_path)
    declarations = VaultProjector(VAULT).declarations()

    observable = tuple(item for item in declarations if item.observable)
    unobservable = tuple(item for item in declarations if not item.observable)
    assert len(packet.assumptions) == len(observable) + 1
    assert len(packet.confounds) == len(unobservable) + 1
    assert any(
        assumption.statement == scenario.fairness.why_neutral.strip()
        for assumption in packet.assumptions
    )
    assert any(
        confound.statement == scenario.fairness.public_coverage_subtraction.strip()
        for confound in packet.confounds
    )
    assert all(assumption.evidence for assumption in packet.assumptions)


def test_a_product_win_a_control_also_scores_is_flagged(tmp_path: Path) -> None:
    """R5, the positive half: the no_product_signal masking rule."""

    packet = _packet(tmp_path, product_outcome="pass", control_outcome="pass")
    assert packet.suspicious_win_flags
    flag = packet.suspicious_win_flags[0]
    assert flag.provider == "exomem"
    assert flag.variant == "exomem-native"
    assert flag.outcome == "pass"
    assert flag.signal_disposition == "no_product_signal"
    assert flag.controls_scoring == ("grep-markdown", "no-memory")


def test_a_product_win_no_control_reproduces_is_not_flagged(tmp_path: Path) -> None:
    """R5, the negative half."""

    packet = _packet(tmp_path, product_outcome="pass", control_outcome="fail")
    assert packet.suspicious_win_flags == ()


def test_every_reviewer_question_is_bound_to_an_artifact_under_the_run_root(
    tmp_path: Path,
) -> None:
    """R7. The count is the contract document's own, read at assert time."""

    from benchmarks.reports.adversarial import REVIEWER_CHALLENGES

    questions = _doc_reviewer_questions()
    assert tuple(challenge.question for challenge in REVIEWER_CHALLENGES) == questions

    packet = _packet(tmp_path)
    assert len(packet.challenge_paths) == len(questions)
    assert tuple(path.question for path in packet.challenge_paths) == questions
    for bound in packet.challenge_paths:
        assert bound.artifact_path
        assert not Path(bound.artifact_path).is_absolute()
        assert ".." not in Path(bound.artifact_path).parts


def test_a_challenge_without_a_bound_artifact_refuses(tmp_path: Path) -> None:
    from benchmarks.reports.adversarial import (
        REVIEWER_CHALLENGES,
        AdversarialPacketError,
    )

    artifacts = _challenge_artifacts()
    dropped = REVIEWER_CHALLENGES[-1].challenge_id
    artifacts.pop(dropped)
    with pytest.raises(AdversarialPacketError) as excinfo:
        _packet(tmp_path, challenge_artifacts=artifacts)
    assert dropped in str(excinfo.value)


def test_a_challenge_bound_outside_the_run_root_refuses(tmp_path: Path) -> None:
    from benchmarks.reports.adversarial import (
        REVIEWER_CHALLENGES,
        AdversarialPacketError,
    )

    artifacts = _challenge_artifacts()
    artifacts[REVIEWER_CHALLENGES[0].challenge_id] = "/var/tmp/challenge.md"
    with pytest.raises(AdversarialPacketError, match="relative"):
        _packet(tmp_path, challenge_artifacts=artifacts)


def _disposition(
    packet,
    *,
    objections=(),
    reviewer_id: str = "independent-reviewer:lane-l3",
    reviewed_at: str = "2026-09-02",
    packet_sha256: str | None = None,
):
    from benchmarks.reports.adversarial import ReviewDisposition, packet_content_sha256

    return ReviewDisposition(
        reviewer_id=reviewer_id,
        reviewed_at=reviewed_at,
        packet_sha256=packet_content_sha256(packet) if packet_sha256 is None else packet_sha256,
        objections=tuple(objections),
    )


def test_an_unreviewed_packet_renders_internal_diagnostic(tmp_path: Path) -> None:
    """R6 case 1 / spec :56-59 — no recorded disposition is not publishable."""

    from benchmarks.reports.adversarial import INTERNAL_DIAGNOSTIC, render_adversarial_packet

    packet = _packet(tmp_path)
    assert packet.review_disposition is None
    # Pinned as a literal, not through the constant: asserting the constant
    # against a render built from that same constant moves both sides at once
    # and would survive a rename of the label the spec names.
    assert INTERNAL_DIAGNOSTIC == "internal-diagnostic"
    assert "internal-diagnostic" in render_adversarial_packet(packet)


def test_a_review_bound_to_other_bytes_is_no_review(tmp_path: Path) -> None:
    """R6 case 2 — a stale review is not a review of *this* packet."""

    from benchmarks.reports.adversarial import INTERNAL_DIAGNOSTIC, render_adversarial_packet

    packet = _packet(tmp_path)
    stale = packet.model_copy(
        update={"review_disposition": _disposition(packet, packet_sha256="0" * 64)}
    )
    assert INTERNAL_DIAGNOSTIC == "internal-diagnostic"
    assert "internal-diagnostic" in render_adversarial_packet(stale)


def test_a_matching_disposition_renders_reviewed_with_its_objections(tmp_path: Path) -> None:
    """R6 case 3 / spec :48-54 — reviewer, date, and every objection's status."""

    from benchmarks.reports.adversarial import (
        Objection,
        render_adversarial_packet,
    )

    packet = _packet(tmp_path)
    objections = (
        Objection(text="the glue LOC asymmetry is not disclosed per row", status="fixed"),
        Objection(text="this host cannot validate a latency comparison", status="documented"),
    )
    reviewed = packet.model_copy(
        update={"review_disposition": _disposition(packet, objections=objections)}
    )
    rendered = render_adversarial_packet(reviewed)
    assert "internal-diagnostic" not in rendered
    assert "independent-reviewer:lane-l3" in rendered
    assert "2026-09-02" in rendered
    for objection in objections:
        assert objection.text in rendered
        assert objection.status in rendered


def test_the_reviewed_hash_covers_the_packet_without_its_own_disposition(
    tmp_path: Path,
) -> None:
    """Otherwise attaching the review would invalidate the hash it just recorded."""

    from benchmarks.reports.adversarial import packet_content_sha256

    packet = _packet(tmp_path)
    before = packet_content_sha256(packet)
    reviewed = packet.model_copy(update={"review_disposition": _disposition(packet)})
    assert packet_content_sha256(reviewed) == before
    assert reviewed.review_disposition.packet_sha256 == before


@pytest.mark.parametrize(
    "reviewer_id",
    [
        "Some Reviewer",
        "hugo.kivi",
        "hugo_kivi",
        "hugo",
        "a@b.com",
        "reviewer:l3",
        "independent-reviewer:",
        "independent-reviewer:Some-Lane",
    ],
)
def test_only_the_allowed_reviewer_handle_form_is_accepted(
    tmp_path: Path, reviewer_id: str
) -> None:
    """D4: an allowlisted FORM, because there is no personal-name detector.

    The repository privacy gate looks for absolute paths and Windows SIDs only
    (``src/exomem/public_artifact_privacy.py``), and the handle renders verbatim
    into the packet markdown — so a character-shape filter that merely rejects
    spaces would pass ``hugo.kivi`` and ``a@b.com`` straight through.
    """

    packet = _packet(tmp_path)
    with pytest.raises(ValidationError, match="independent-reviewer:"):
        _disposition(packet, reviewer_id=reviewer_id)


def test_the_allowed_reviewer_handle_form_is_accepted(tmp_path: Path) -> None:
    packet = _packet(tmp_path)
    disposition = _disposition(packet, reviewer_id="independent-reviewer:l3-fairness")
    assert disposition.reviewer_id == "independent-reviewer:l3-fairness"


@pytest.mark.parametrize("reviewed_at", ["2026-13-45", "2026-02-30", "2026-00-10"])
def test_an_impossible_review_date_refuses(tmp_path: Path, reviewed_at: str) -> None:
    """A well-shaped string is not a date; the calendar has to accept it."""

    packet = _packet(tmp_path)
    with pytest.raises(ValidationError, match="calendar date"):
        _disposition(packet, reviewed_at=reviewed_at)


def test_the_rendered_packet_publishes_the_preregistration_hash(tmp_path: Path) -> None:
    from benchmarks.reports.adversarial import render_adversarial_packet

    packet = _packet(tmp_path)
    rendered = render_adversarial_packet(packet)
    assert packet.preregistration.sha256 in rendered
    for bound in packet.challenge_paths:
        assert bound.artifact_path in rendered


def test_rendering_refuses_free_text_that_carries_an_aggregate(tmp_path: Path) -> None:
    """Integration fold: a reviewer's objection text reaches the page verbatim."""

    from benchmarks.reports.adversarial import Objection, render_adversarial_packet
    from benchmarks.reports.guards import ReportRefused

    packet = _packet(tmp_path)
    reviewed = packet.model_copy(
        update={
            "review_disposition": _disposition(
                packet,
                objections=(
                    Objection(
                        text="the aggregate MemScore is quoted in the summary",
                        status="documented",
                    ),
                ),
            )
        }
    )
    with pytest.raises(ReportRefused, match="never publish an aggregate"):
        render_adversarial_packet(reviewed)
