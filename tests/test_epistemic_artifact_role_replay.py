"""Sequence-five replay contracts: authored inputs, delivery and state settlement."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "benchmarks/epistemic/fixtures/sequence5"
PRIOR_RECEIPT_SHA256S = {
    "amendment-2026-08-loop-closure.v1.json": (
        "9bd911d666be9d89e5fb33f387517886ba51b74dd3264ed4e28f8d1a9f42fa40"
    ),
    "amendment-2026-08-no-nudge.v1.json": (
        "e92804fc315f93fbc2404e6a8e4ad966c4bc50713d5c38424f99cea92e949e47"
    ),
    "amendment-2026-08-lifecycle-replay.v1.json": (
        "940ba026a9ab101230aa4d09b22fa676fb88f90856975546843059939cee1475"
    ),
    "amendment-2026-09-collection-claims.v1.json": (
        "ba616719a98092328ad14662c54a230a3e7e1e259706473fe5897557dd3641f8"
    ),
}


def test_corpora_are_deterministic_and_have_discriminating_twins() -> None:
    from epistemic.journeys.role_state_replay import ORIGIN, corpus_for, replay_corpora

    corpora = replay_corpora()
    assert {corpus.corpus_id for corpus in corpora} == {
        "f30-role-positive-v1",
        "f30-protocol-only-v1",
        "f30-already-promoted-v1",
        "f31-current-pending-v1",
        "f31-historical-v1",
        "f31-different-trial-v1",
    }
    assert corpora == replay_corpora()
    assert all(corpus_for(corpus.corpus_id) == corpus for corpus in corpora)
    positive = corpus_for("f30-role-positive-v1")
    assert positive.expected_roles == ("method-a", "method-b", "synthesis")
    assert positive.family_id == "f30"
    assert corpus_for("f30-protocol-only-v1").expected_roles == ()
    assert corpus_for("f30-already-promoted-v1").expected_roles == ()
    assert corpus_for("f31-current-pending-v1").expect_transient
    assert not corpus_for("f31-historical-v1").expect_transient
    assert not corpus_for("f31-different-trial-v1").expect_transient
    assert all(any(turn.quiet for turn in corpus.turns) for corpus in corpora)
    assert "worked well" not in dict(positive.seed_pages)[ORIGIN]
    assert "  Hold the strainer steady." in dict(positive.seed_pages)[ORIGIN]
    assert "worked well" in positive.expert_origin
    pending = corpus_for("f31-current-pending-v1")
    assert "Taste results: worked well" not in dict(pending.seed_pages)[ORIGIN]
    assert "Taste results: worked well" in pending.expert_origin
    protocol_turns = " ".join(
        turn.text.lower() for turn in corpus_for("f30-protocol-only-v1").turns
    )
    assert "reuse" not in protocol_turns
    assert "repeatable" not in protocol_turns
    assert "taken together" not in protocol_turns
    historical_turns = " ".join(turn.text.lower() for turn in corpus_for("f31-historical-v1").turns)
    assert "before trial a was tasted" in historical_turns
    different_turns = " ".join(
        turn.text.lower() for turn in corpus_for("f31-different-trial-v1").turns
    )
    assert "trial b's tasting result" in different_turns
    assert "trial a still has no taste result" in different_turns


@pytest.mark.parametrize("phrase", ["save this one", "put it in Exomem", "track this"])
def test_corpus_construction_refuses_storage_commands(phrase: str) -> None:
    from epistemic.journeys.role_state_replay import StoreBearingUtterance, build_corpus, corpus_for

    corpus = corpus_for("f30-role-positive-v1")
    bad = replace(corpus.turns[0], text=phrase)
    with pytest.raises(StoreBearingUtterance, match=bad.turn_id):
        build_corpus(replace(corpus, turns=(bad, *corpus.turns[1:])))


def test_checked_in_fixtures_pin_every_turn_and_seed() -> None:
    from epistemic.journeys.role_state_replay import fixture_payload, replay_corpora

    for corpus in replay_corpora():
        assert corpus.digest() == corpus.digest()
        payload = yaml.safe_load((FIXTURES / f"{corpus.corpus_id}.yaml").read_text())
        assert payload == fixture_payload(corpus)
        for arm in ("hookless", "hooked"):
            turns = [
                (op["ref"], op["detail"])
                for phase in payload["phases"]
                if phase["phase_id"].startswith(arm)
                for op in phase["ops"]
                if op["op"] == "agent_turn"
            ]
            assert turns == [(turn.turn_id, turn.text) for turn in corpus.turns]


def test_loader_refuses_store_command_and_clean_turn_drift(monkeypatch: pytest.MonkeyPatch) -> None:
    from epistemic.journeys.role_state_replay import corpus_for, fixture_payload
    from epistemic.schema import ScenarioLoadError, load_scenario_text

    monkeypatch.setattr("epistemic.schema.require_family_released", lambda _family: None)
    corpus = corpus_for("f30-role-positive-v1")
    payload = fixture_payload(corpus)
    assert load_scenario_text(json.dumps(payload), source="pinned").scenario_id == corpus.corpus_id
    for wording, fragment in (
        ("save this one", "store-bearing"),
        ("That result was nice.", "turn drift"),
    ):
        changed = json.loads(json.dumps(payload))
        changed["phases"][0]["ops"][2]["detail"] = wording
        with pytest.raises(ScenarioLoadError, match=fragment):
            load_scenario_text(json.dumps(changed), source="mutated")


def test_sequence_five_receipt_and_all_prior_identity_bytes() -> None:
    from epistemic.amendments import withheld_family_ids
    from epistemic.registry import AMENDMENT_INTRODUCED_FAMILIES, PREREGISTERED_FAMILY_IDS
    from protocol.contracts import working_amendment_receipts

    for filename, expected in PRIOR_RECEIPT_SHA256S.items():
        receipt_bytes = (ROOT / "benchmarks/epistemic/contracts" / filename).read_bytes()
        # A later founder acknowledgment legitimately changes pending receipt
        # bytes; their pinned contract identities still remain in the chain.
        if json.loads(receipt_bytes).get("ratifier") is None:
            assert hashlib.sha256(receipt_bytes).hexdigest() == expected
    chain = working_amendment_receipts(ROOT)
    assert [row.sequence for row in chain] == [1, 2, 3, 4, 5]
    assert chain[-1].parent_contract_sha256 == chain[-2].contract_sha256
    assert (
        chain[-1].contract_sha256
        == hashlib.sha256(
            (ROOT / "benchmarks/epistemic/PREREGISTRATION.md").read_bytes()
        ).hexdigest()
    )
    assert chain[-1].acknowledgment_status == "pending"
    assert {"f30", "f31"} <= withheld_family_ids()
    assert {"f30", "f31"} <= PREREGISTERED_FAMILY_IDS
    assert {family: AMENDMENT_INTRODUCED_FAMILIES[family] for family in ("f30", "f31")} == {
        "f30": 5,
        "f31": 5,
    }


@pytest.mark.parametrize("family", ["f30", "f31"])
def test_pending_family_refused_at_load_evaluation_and_manifest(
    family: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from epistemic.journeys.role_state_replay import corpus_for, fixture_payload
    from epistemic.manifest import start_epistemic_manifest
    from epistemic.runner import evaluate_scenario
    from epistemic.schema import Scenario, ScenarioLoadError, load_scenario_text
    from protocol.contracts import (
        AmendmentAcknowledgmentPendingError,
        derive_preregistration_identity,
    )
    from protocol.manifest import ManifestError

    corpus = corpus_for("f30-role-positive-v1" if family == "f30" else "f31-current-pending-v1")
    payload = fixture_payload(corpus)
    with pytest.raises(ScenarioLoadError, match=r"sequence 5.*pending"):
        load_scenario_text(json.dumps(payload), source=corpus.corpus_id)
    scenario = Scenario.model_validate_json(json.dumps(payload))
    with pytest.raises(AmendmentAcknowledgmentPendingError, match=r"sequence 5.*pending"):
        evaluate_scenario(scenario, snapshots={})
    # The Git-derived identity sees the new receipt only after the orchestrator
    # commits this worktree. Pin a pending identity here to exercise the manifest
    # gate before delivery; the real chain bytes are asserted above.
    base = derive_preregistration_identity(ROOT)
    receipt = json.loads(
        (
            ROOT / "benchmarks/epistemic/contracts/amendment-2026-09-role-state-review.v1.json"
        ).read_text()
    )
    fifth = base.amendments[-1].model_copy(
        update={
            "sequence": 5,
            "parent_contract_sha256": base.effective.sha256,
            "contract": base.effective.model_copy(update={"sha256": receipt["contract_sha256"]}),
            "acknowledgment_status": "pending",
            "introduced_family_ids": (family,),
        }
    )
    identity = base.model_copy(
        update={"amendments": (*base.amendments, fifth), "effective": fifth.contract}
    )
    monkeypatch.setattr(
        "protocol.manifest.validate_working_preregistration", lambda *_: receipt["contract_sha256"]
    )
    monkeypatch.setattr(
        "protocol.manifest.derive_preregistration_identity", lambda *_, **__: identity
    )
    with pytest.raises(ManifestError, match=r"sequence 5.*pending"):
        start_epistemic_manifest(
            tmp_path / "run",
            scenarios=(scenario,),
            run_id="role-state-probe",
            dataset={
                "id": "fixture",
                "variant": "mini",
                "source": "local",
                "revision": "1",
                "sha256": "a" * 64,
                "case_count": 1,
            },
            started_at="2026-09-12T00:00:00Z",
        )
    assert not (tmp_path / "run" / "manifest.json").exists()


def _snapshot(corpus_id: str, *, carrier: bool = False, count: int = 0, internal: bool = False):
    from epistemic.journeys.role_state_replay import ORIGIN, corpus_for
    from epistemic.snapshot import EpistemicStateSnapshot, ProjectorMeta, StateItem

    corpus = corpus_for(corpus_id)
    category = "artifact_role_promotion" if corpus.family_id == "f30" else "transient_state_review"
    items = [
        StateItem(
            id=path.removesuffix(".md"),
            kind="hypothesis" if path == ORIGIN else "raw_source",
            text=body,
            locator=path,
            current="yes",
            raw={
                "type": "experiment" if path == ORIGIN else "source",
                "exomem_id": "1f6d297a-28e7-4aa2-8cc7-62b0da294200" if path == ORIGIN else "",
            },
        )
        for path, body in corpus.seed_pages
        if path == ORIGIN or "/Sources/" in path
    ]
    items.extend(
        StateItem(
            id=f"surface-{surface}",
            kind="container",
            raw={"surface": surface, "projection": "complete"},
        )
        for surface in ("audit_findings", "review_queue", "proposal_queue", "due_state_counters")
    )
    if internal:
        items.append(
            StateItem(
                id="projected-only",
                kind="container",
                raw={
                    "surface": "audit_findings",
                    "category": category,
                    "targets": ORIGIN,
                },
            )
        )
    if carrier:
        memory = "exomem://memory/1f6d297a-28e7-4aa2-8cc7-62b0da294200"
        evidence = (
            (
                ("reusable_method", [f"{memory}#method-a", f"{memory}#outcome-a"]),
                ("reusable_method", [f"{memory}#method-b", f"{memory}#outcome-b"]),
                ("research_synthesis", [f"{memory}#synthesis"]),
            )
            if corpus.family_id == "f30"
            else (("current_pending_with_result", [f"{memory}#pending", f"{memory}#outcome-a"]),)
        )
        review_evidence = {
            f"exomem://review/{index}": {"role": role, "evidence_refs": refs}
            for index, (role, refs) in enumerate(evidence[:count])
        }
        items.append(
            StateItem(
                id="delivered",
                kind="container",
                raw={
                    "surface": "client_carrier",
                    "category": category,
                    "targets": ORIGIN,
                    "after_write": "true",
                    "count": str(count),
                    "review_refs": json.dumps(
                        [f"exomem://review/{index}" for index in range(count)]
                    ),
                    "review_evidence": json.dumps(review_evidence),
                },
            )
        )
    return EpistemicStateSnapshot(
        provider="fixture",
        phase="hookless-evidence",
        taken_at="2026-09-12T00:00:00Z",
        items=tuple(items),
        projector=ProjectorMeta(
            name="fixture", version="1", author="test", endpoints_used=("fixture",), loc=1
        ),
    )


def test_role_delivery_requires_actual_post_write_client_carrier() -> None:
    from epistemic.assertions import AssertionContext, role_signal_delivered_after_write

    subject = "f30-role-positive-v1"
    baseline = _snapshot(subject, internal=True)
    assert (
        role_signal_delivered_after_write(AssertionContext(baseline, subject=subject)).outcome
        == "fail"
    )
    assert (
        role_signal_delivered_after_write(
            AssertionContext(_snapshot(subject, carrier=True, count=2), subject=subject)
        ).outcome
        == "fail"
    )
    assert (
        role_signal_delivered_after_write(
            AssertionContext(_snapshot(subject, carrier=True, count=3), subject=subject)
        ).outcome
        == "pass"
    )
    wrong_refs = _snapshot(subject, carrier=True, count=3)
    carrier = wrong_refs.items[-1]
    forged = carrier.model_copy(
        update={
            "raw": {
                **carrier.raw,
                "review_refs": json.dumps([f"exomem://review/unrelated-{i}" for i in range(3)]),
            }
        }
    )
    wrong_refs = wrong_refs.model_copy(update={"items": (*wrong_refs.items[:-1], forged)})
    assert (
        role_signal_delivered_after_write(AssertionContext(wrong_refs, subject=subject)).outcome
        == "fail"
    )


@pytest.mark.parametrize("subject", ["f30-protocol-only-v1", "f30-already-promoted-v1"])
def test_role_controls_require_projected_quiet(subject: str) -> None:
    from epistemic.assertions import AssertionContext, role_signal_delivered_after_write

    assert (
        role_signal_delivered_after_write(
            AssertionContext(_snapshot(subject), subject=subject)
        ).outcome
        == "pass"
    )
    assert (
        role_signal_delivered_after_write(
            AssertionContext(_snapshot(subject, internal=True), subject=subject)
        ).outcome
        == "fail"
    )


def test_transient_delivery_requires_carrier_and_control_quiet() -> None:
    from epistemic.assertions import AssertionContext, transient_signal_delivered_after_write

    subject = "f31-current-pending-v1"
    assert (
        transient_signal_delivered_after_write(
            AssertionContext(_snapshot(subject, internal=True), subject=subject)
        ).outcome
        == "fail"
    )
    assert (
        transient_signal_delivered_after_write(
            AssertionContext(_snapshot(subject, carrier=True, count=1), subject=subject)
        ).outcome
        == "pass"
    )
    for twin in ("f31-historical-v1", "f31-different-trial-v1"):
        assert (
            transient_signal_delivered_after_write(
                AssertionContext(_snapshot(twin), subject=twin)
            ).outcome
            == "pass"
        )
        assert (
            transient_signal_delivered_after_write(
                AssertionContext(_snapshot(twin, internal=True), subject=twin)
            ).outcome
            == "fail"
        )


def test_projected_units_preserve_exact_text_refs_and_relation_targets(tmp_path: Path) -> None:
    from epistemic.journeys.role_state_replay import corpus_for, seed_inputs
    from epistemic.journeys.role_state_replay_driver import project_replay_snapshot

    corpus = corpus_for("f30-already-promoted-v1")
    seed_inputs(corpus, tmp_path)
    snapshot = project_replay_snapshot(
        tmp_path, phase="hookless-control", taken_at="2026-09-12T00:00:00Z"
    )
    units = {item.raw["unit_ref"]: item for item in snapshot.items if "unit_ref" in item.raw}
    source = next(item for ref, item in units.items() if ref.endswith("#method-a"))
    extracted = next(
        item
        for item in units.values()
        if item.raw.get("parent_path", "").endswith("steeping-method.md")
    )
    assert (
        source.text
        == extracted.text
        == ("Reusable method: steep at 82 C for four minutes.\n  Hold the strainer steady.")
    )
    assert any(target.endswith("#method-a") for target in extracted.cites)
    assert any(target.endswith("#outcome-a") for target in extracted.cites)


def test_role_settlement_requires_all_three_represented_artifacts(tmp_path: Path) -> None:
    from epistemic.assertions import AssertionContext, role_state_settled_with_provenance
    from epistemic.journeys.role_state_replay import ORIGIN, corpus_for, seed_inputs
    from epistemic.journeys.role_state_replay_driver import project_replay_snapshot
    from epistemic.snapshot import StateItem

    positive = corpus_for("f30-role-positive-v1")
    promoted = corpus_for("f30-already-promoted-v1")
    seed_inputs(positive, tmp_path)
    (tmp_path / ORIGIN).write_text(positive.expert_origin)
    prior = project_replay_snapshot(
        tmp_path, phase="hookless-settlement", taken_at="2026-09-12T00:00:00Z"
    )
    for path, body in promoted.seed_pages[len(positive.seed_pages) :]:
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body)
    final = project_replay_snapshot(
        tmp_path, phase="hookless-settlement", taken_at="2026-09-12T00:00:01Z"
    )
    authority = StateItem(
        id="authorized-action",
        kind="container",
        raw={
            "surface": "client_action",
            "authority": "consent",
            "operation": "edit_memory",
            "turn_id": "t05-consent",
            "targets": "Knowledge Base/Notes/Experiments/tea-trials.md",
        },
    )
    final = final.model_copy(update={"items": (*final.items, authority)})
    context = AssertionContext(final, prior=prior, subject=positive.corpus_id)
    assert role_state_settled_with_provenance(context).outcome == "pass"

    def without_second_origin_source(snapshot):
        return snapshot.model_copy(
            update={
                "items": tuple(
                    item.model_copy(update={"cites": item.cites[:1]})
                    if item.raw.get("unit_ref", "").endswith("#synthesis")
                    else item
                    for item in snapshot.items
                )
            }
        )

    unattributed = AssertionContext(
        without_second_origin_source(final),
        prior=without_second_origin_source(prior),
        subject=positive.corpus_id,
    )
    assert role_state_settled_with_provenance(unattributed).outcome == "fail"
    wrong_action = authority.model_copy(
        update={"raw": {**authority.raw, "targets": "Knowledge Base/Notes/Insights/unrelated.md"}}
    )
    unrelated = final.model_copy(update={"items": (*final.items[:-1], wrong_action)})
    assert (
        role_state_settled_with_provenance(
            AssertionContext(unrelated, prior=prior, subject=positive.corpus_id)
        ).outcome
        == "fail"
    )
    dismissed = StateItem(
        id="dismissed",
        kind="container",
        review_state="dismissed",
        raw={
            "surface": "review_queue",
            "signal_categories": "artifact_role_promotion",
            "targets": ORIGIN,
        },
    )
    dismissed_state = final.model_copy(update={"items": (*final.items, dismissed)})
    assert (
        role_state_settled_with_provenance(
            AssertionContext(dismissed_state, prior=prior, subject=positive.corpus_id)
        ).outcome
        == "fail"
    )
    destination = tmp_path / "Knowledge Base/Notes/Patterns/steeping-method.md"
    original = destination.read_text()
    destination.write_text(original.replace("82 C", "82 c"))
    changed = project_replay_snapshot(
        tmp_path, phase="hookless-settlement", taken_at="2026-09-12T00:00:02Z"
    )
    changed = changed.model_copy(update={"items": (*changed.items, authority)})
    assert (
        role_state_settled_with_provenance(
            AssertionContext(changed, prior=prior, subject=positive.corpus_id)
        ).outcome
        == "fail"
    )
    destination.write_text(original.replace("type: pattern", "type: experiment"))
    wrong_role = project_replay_snapshot(
        tmp_path, phase="hookless-settlement", taken_at="2026-09-12T00:00:03Z"
    )
    wrong_role = wrong_role.model_copy(update={"items": (*wrong_role.items, authority)})
    assert (
        role_state_settled_with_provenance(
            AssertionContext(wrong_role, prior=prior, subject=positive.corpus_id)
        ).outcome
        == "fail"
    )
    destination.write_text(original.replace("## Procedure", "## Claim"))
    wrong_unit_role = project_replay_snapshot(
        tmp_path, phase="hookless-settlement", taken_at="2026-09-12T00:00:03Z"
    )
    wrong_unit_role = wrong_unit_role.model_copy(
        update={"items": (*wrong_unit_role.items, authority)}
    )
    assert (
        role_state_settled_with_provenance(
            AssertionContext(wrong_unit_role, prior=prior, subject=positive.corpus_id)
        ).outcome
        == "fail"
    )
    destination.write_text(original)
    synthesis_destination = tmp_path / "Knowledge Base/Notes/Research/tea-extraction.md"
    original_synthesis = synthesis_destination.read_text()
    synthesis_destination.write_text(original_synthesis.replace("## Finding", "## Claim"))
    wrong_synthesis_role = project_replay_snapshot(
        tmp_path, phase="hookless-settlement", taken_at="2026-09-12T00:00:03Z"
    )
    wrong_synthesis_role = wrong_synthesis_role.model_copy(
        update={"items": (*wrong_synthesis_role.items, authority)}
    )
    assert (
        role_state_settled_with_provenance(
            AssertionContext(wrong_synthesis_role, prior=prior, subject=positive.corpus_id)
        ).outcome
        == "fail"
    )
    synthesis_destination.write_text(original_synthesis)
    destination.write_text(original.replace("  Hold the strainer", " Hold the strainer"))
    wrong_indent = project_replay_snapshot(
        tmp_path, phase="hookless-settlement", taken_at="2026-09-12T00:00:04Z"
    )
    wrong_indent = wrong_indent.model_copy(update={"items": (*wrong_indent.items, authority)})
    assert (
        role_state_settled_with_provenance(
            AssertionContext(wrong_indent, prior=prior, subject=positive.corpus_id)
        ).outcome
        == "fail"
    )


def test_role_settlement_accepts_two_method_units_on_one_page(tmp_path: Path) -> None:
    from epistemic.assertions import AssertionContext, role_state_settled_with_provenance
    from epistemic.journeys.role_state_replay import ORIGIN, corpus_for, seed_inputs
    from epistemic.journeys.role_state_replay_driver import project_replay_snapshot
    from epistemic.snapshot import StateItem

    from exomem import audit

    positive = corpus_for("f30-role-positive-v1")
    promoted = corpus_for("f30-already-promoted-v1")
    seed_inputs(positive, tmp_path)
    (tmp_path / ORIGIN).write_text(positive.expert_origin)
    prior = project_replay_snapshot(
        tmp_path, phase="hookless-settlement", taken_at="2026-09-12T00:00:00Z"
    )
    destinations = dict(promoted.seed_pages[len(positive.seed_pages) :])
    first_path = "Knowledge Base/Notes/Patterns/steeping-method.md"
    second_path = "Knowledge Base/Notes/Patterns/cooling-method.md"
    synthesis_path = "Knowledge Base/Notes/Research/tea-extraction.md"
    second_unit = (
        destinations[second_path]
        .split("---\n\n", 1)[1]
        .replace("id: extracted", "id: extracted-second")
    )
    for path, body in (
        (first_path, destinations[first_path] + "\n" + second_unit),
        (synthesis_path, destinations[synthesis_path]),
    ):
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body)
    assert not audit.audit(tmp_path, categories=["artifact_role_promotion"]).findings
    final = project_replay_snapshot(
        tmp_path, phase="hookless-settlement", taken_at="2026-09-12T00:00:01Z"
    )
    authority = StateItem(
        id="authorized-action",
        kind="container",
        raw={
            "surface": "client_action",
            "authority": "consent",
            "operation": "edit_memory",
            "turn_id": "t05-consent",
            "targets": ORIGIN,
        },
    )
    final = final.model_copy(update={"items": (*final.items, authority)})
    assert (
        role_state_settled_with_provenance(
            AssertionContext(final, prior=prior, subject=positive.corpus_id)
        ).outcome
        == "pass"
    )


@pytest.mark.parametrize("subject", ["f31-historical-v1", "f31-different-trial-v1"])
def test_quiet_transient_twins_preserve_authored_pending_unit(tmp_path: Path, subject: str) -> None:
    from epistemic.assertions import AssertionContext, transient_state_settled_without_dismissal
    from epistemic.journeys.role_state_replay import ORIGIN, corpus_for, seed_inputs
    from epistemic.journeys.role_state_replay_driver import project_replay_snapshot
    from epistemic.snapshot import StateItem

    corpus = corpus_for(subject)
    seed_inputs(corpus, tmp_path)
    prior = project_replay_snapshot(
        tmp_path, phase="hookless-control", taken_at="2026-09-12T00:00:00Z"
    )
    assert (
        transient_state_settled_without_dismissal(
            AssertionContext(prior, prior=prior, subject=subject)
        ).outcome
        == "pass"
    )
    dismissal = StateItem(
        id="invented-dismissal",
        kind="container",
        review_state="dismissed",
        raw={
            "surface": "review_queue",
            "category": "transient_state_review",
            "targets": ORIGIN,
        },
    )
    dismissed = prior.model_copy(update={"items": (*prior.items, dismissal)})
    assert (
        transient_state_settled_without_dismissal(
            AssertionContext(dismissed, prior=prior, subject=subject)
        ).outcome
        == "fail"
    )
    origin = tmp_path / ORIGIN
    body = origin.read_text()
    claim_start = body.index("## Claim\n")
    result_start = body.index("## Result\n", claim_start)
    origin.write_text(body[:claim_start] + body[result_start:])
    deleted = project_replay_snapshot(
        tmp_path, phase="hookless-control", taken_at="2026-09-12T00:00:01Z"
    )
    assert (
        transient_state_settled_without_dismissal(
            AssertionContext(deleted, prior=prior, subject=subject)
        ).outcome
        == "fail"
    )


def test_transient_settlement_is_state_based_and_rejects_false_writes(tmp_path: Path) -> None:
    from epistemic.assertions import AssertionContext, transient_state_settled_without_dismissal
    from epistemic.journeys.role_state_replay import ORIGIN, corpus_for, seed_inputs
    from epistemic.journeys.role_state_replay_driver import project_replay_snapshot
    from epistemic.snapshot import StateItem

    corpus = corpus_for("f31-current-pending-v1")
    seed_inputs(corpus, tmp_path)
    (tmp_path / ORIGIN).write_text(corpus.expert_origin)
    prior = project_replay_snapshot(
        tmp_path, phase="hookless-settlement", taken_at="2026-09-12T00:00:00Z"
    )
    assert (
        transient_state_settled_without_dismissal(
            AssertionContext(prior, prior=prior, subject=corpus.corpus_id)
        ).outcome
        == "fail"
    )
    origin = tmp_path / ORIGIN
    origin.write_text(
        origin.read_text().replace("No taste results yet.", "Previously, no taste results yet.")
    )
    corrected = project_replay_snapshot(
        tmp_path, phase="hookless-settlement", taken_at="2026-09-12T00:00:01Z"
    )
    authority = StateItem(
        id="authorized-action",
        kind="container",
        raw={
            "surface": "client_action",
            "authority": "consent",
            "operation": "edit_memory",
            "turn_id": "t03-consent",
            "targets": ORIGIN,
        },
    )
    corrected = corrected.model_copy(update={"items": (*corrected.items, authority)})
    assert (
        transient_state_settled_without_dismissal(
            AssertionContext(corrected, prior=prior, subject=corpus.corpus_id)
        ).outcome
        == "pass"
    )
    observed_action = authority.model_copy(
        update={"raw": {**authority.raw, "operation": "observe_memory"}}
    )
    observed_correction = corrected.model_copy(
        update={"items": (*corrected.items[:-1], observed_action)}
    )
    assert (
        transient_state_settled_without_dismissal(
            AssertionContext(observed_correction, prior=prior, subject=corpus.corpus_id)
        ).outcome
        == "pass"
    )
    spurious = tmp_path / "Knowledge Base/Notes/Insights/spurious.md"
    spurious.parent.mkdir(parents=True, exist_ok=True)
    spurious.write_text("---\ntype: insight\n---\n\nThe untried variation succeeded.\n")
    extra = project_replay_snapshot(
        tmp_path, phase="hookless-settlement", taken_at="2026-09-12T00:00:02Z"
    )
    extra = extra.model_copy(update={"items": (*extra.items, authority)})
    assert (
        transient_state_settled_without_dismissal(
            AssertionContext(extra, prior=prior, subject=corpus.corpus_id)
        ).outcome
        == "fail"
    )
    spurious.unlink()
    origin.write_text(
        origin.read_text() + "\n## Result\n- id: invented\n\nA variation might succeed.\n"
    )
    false_unit = project_replay_snapshot(
        tmp_path, phase="hookless-settlement", taken_at="2026-09-12T00:00:03Z"
    )
    false_unit = false_unit.model_copy(update={"items": (*false_unit.items, authority)})
    assert (
        transient_state_settled_without_dismissal(
            AssertionContext(false_unit, prior=prior, subject=corpus.corpus_id)
        ).outcome
        == "fail"
    )


def test_carrier_witness_comes_from_successful_write_tool_result() -> None:
    from epistemic.journeys.role_state_replay import ORIGIN
    from epistemic.journeys.role_state_replay_driver import carrier_items_from_transcript

    block = {
        "due_state": {
            "total": 3,
            "categories": {"artifact_role_promotion": 3},
            "top": [
                {"category": "artifact_role_promotion", "ref": f"exomem://review/{i}"}
                for i in range(3)
            ],
        },
        "status": "committed",
    }

    def stream(*, tool="mcp__exomem__edit_memory", result=True):
        rows = [
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "write-1",
                            "name": tool,
                            "input": {"path": ORIGIN},
                        }
                    ]
                },
            }
        ]
        if result:
            rows.append(
                {
                    "type": "user",
                    "message": {
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "write-1",
                                "content": json.dumps(block),
                            }
                        ]
                    },
                }
            )
        rows.append({"type": "result", "subtype": "success", "result": "Finished."})
        return "\n".join(map(json.dumps, rows))

    review_index = {
        f"exomem://review/{i}": {
            "category": "artifact_role_promotion",
            "path": ORIGIN,
            "role": "reusable_method" if i < 2 else "research_synthesis",
            "evidence_refs": [f"exomem://memory/id#method-{i}"],
        }
        for i in range(3)
    }
    delivered = carrier_items_from_transcript(
        stream(), turn_id="t05-consent", review_index=review_index
    )
    assert len(delivered) == 2
    assert delivered[0].raw["surface"] == "client_carrier"
    assert delivered[0].raw["count"] == "3"
    assert delivered[1].raw["surface"] == "client_action"
    assert (
        carrier_items_from_transcript(
            stream(tool="mcp__exomem__read_memory"),
            turn_id="t05-consent",
            review_index=review_index,
        )
        == ()
    )
    assert (
        carrier_items_from_transcript(
            stream(result=False), turn_id="t05-consent", review_index=review_index
        )
        == ()
    )


def test_native_runner_classifies_isolation_refusal_without_a_product_score(tmp_path: Path) -> None:
    from epistemic.journeys.f27_replay import AgentEnvelope
    from epistemic.journeys.role_state_replay import corpus_for
    from epistemic.journeys.role_state_replay_driver import run_development

    report = run_development(
        corpus_for("f30-role-positive-v1"),
        out_dir=tmp_path / "unsafe",
        taken_at="2026-09-12T00:00:00Z",
        envelope=AgentEnvelope(Path("/usr/bin/false"), "fake"),
    )
    assert report["score"] is None
    assert [arm["arm"] for arm in report["arms"]] == ["hookless", "hooked"]
    assert all(arm["harness_fault"] and arm["turns_executed"] == 0 for arm in report["arms"])
    assert all(not arm["snapshots"] and arm["score"] is None for arm in report["arms"])
    assert all(
        set(result["outcome"] for result in arm["assertions"].values()) == {"blocked"}
        for arm in report["arms"]
    )


def test_dry_run_prints_complete_native_argv_and_environment_delta(tmp_path: Path) -> None:
    from epistemic.journeys.f27_replay import AgentEnvelope
    from epistemic.journeys.role_state_replay import corpus_for
    from epistemic.journeys.role_state_replay_driver import run_development

    corpus = corpus_for("f31-current-pending-v1")
    report = run_development(
        corpus,
        out_dir=tmp_path / "unsafe",
        taken_at="2026-09-12T00:00:00Z",
        envelope=AgentEnvelope(Path("/usr/bin/false"), "fake"),
        dry_run=True,
        parent_env={"CLAUDECODE": "nested", "PATH": "/usr/bin"},
    )
    assert not (tmp_path / "unsafe").exists()
    for arm in report["arms"]:
        assert len(arm["argv"]) == len(corpus.turns)
        assert all("--strict-mcp-config" in argv and "--max-turns" in argv for argv in arm["argv"])
        assert "CLAUDECODE" in arm["env_delta"][0]


def test_native_runner_refuses_checked_fixture_drift_before_any_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from epistemic.journeys.f27_replay import AgentEnvelope
    from epistemic.journeys.role_state_replay import corpus_for
    from epistemic.journeys.role_state_replay_driver import run_development

    corpus = corpus_for("f30-role-positive-v1")
    (tmp_path / f"{corpus.corpus_id}.yaml").write_text("scenario_id: changed\n")
    monkeypatch.setattr(
        "epistemic.journeys.role_state_replay_driver.FIXTURE_DIR", tmp_path, raising=False
    )
    with pytest.raises(ValueError, match="fixture drift"):
        run_development(
            corpus,
            out_dir=tmp_path / "unused",
            taken_at="2026-09-12T00:00:00Z",
            envelope=AgentEnvelope(Path("/usr/bin/false"), "fake"),
            dry_run=True,
        )
