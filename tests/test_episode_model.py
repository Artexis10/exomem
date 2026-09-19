from __future__ import annotations

import copy

import pytest


def test_start_episode_derives_an_identity() -> None:
    from exomem import episode_model

    state = episode_model.start_episode("turn-42", {"excerpt": "Observation."})

    assert state["episode_id"]


def _input(*, excerpt: str = "A supported observation.") -> dict[str, str]:
    return {"excerpt": excerpt}


def _step(*, suffix: str = "one") -> dict[str, object]:
    return {
        "leaf_key": f"note-{suffix}",
        "effect_revision": 1,
        "kind": "create-note",
        "args": {
            "title": f"Episode note {suffix}",
            "content": chr(10).join(
                ("## Observations", "", "- [finding] A supported observation.", "")
            ),
        },
    }


def _proposal(*steps: dict[str, object]) -> dict[str, object]:
    return {
        "route": "focused_note",
        "title": "Episode note",
        "alternatives": [],
        "evidence": "complete",
        "reason": "This observation needs a focused durable home.",
        "leaves": list(steps or (_step(),)),
    }


def _bound_plan(step: dict[str, object]) -> dict[str, object]:
    from exomem import curation

    plan = curation.validate_forward_plan(
        {
            "version": 1,
            "title": "Episode curation plan",
            "steps": [{"step_id": "write-note", "kind": step["kind"], "args": step["args"]}],
        }
    )
    sealed = {**plan, "binding_manifest": [], "registry_ids": {}}
    plan_id = curation.plan_id(sealed)
    return {
        "sealed_plan": sealed,
        "run_id": f"cur-20260919-{plan_id[:12]}",
        "plan_id": plan_id,
        "plan_fingerprint": curation.plan_fingerprint(plan, [], {}),
        "ordinal": 0,
        "step_id": "write-note",
        "operation_id": curation.operation_id(plan_id, 0, "write-note"),
    }


def _prepared() -> tuple[dict[str, object], str, str, dict[str, object]]:
    from exomem import episode_model

    state = episode_model.start_episode("turn-42", _input())
    state = episode_model.declare_candidate(state, "observation")
    candidate_id = state["candidates"][0]["candidate_id"]
    step = _step()
    state = episode_model.revise_proposal(state, candidate_id, _proposal(step))
    state = episode_model.set_disposition(state, candidate_id, "routed", "Capture it.")
    leaf_id = state["candidates"][0]["leaves"][0]["leaf_id"]
    state = episode_model.bind_curation_leaf(state, candidate_id, leaf_id, _bound_plan(step))
    state = episode_model.attest_precommit(state, 1)
    return state, candidate_id, leaf_id, step


def test_identity_is_stable_across_reordered_candidates_and_leaves() -> None:
    from exomem import episode_model

    first = episode_model.declare_candidate(episode_model.start_episode("turn-42", _input()), "b")
    first = episode_model.declare_candidate(first, "a")
    second = episode_model.declare_candidate(episode_model.start_episode("turn-42", _input()), "a")
    second = episode_model.declare_candidate(second, "b")
    assert first["episode_id"] == second["episode_id"]
    assert first["candidates"] == second["candidates"]
    candidate_id = first["candidates"][0]["candidate_id"]
    proposed = episode_model.revise_proposal(first, candidate_id, _proposal(_step(suffix="a")))
    assert proposed["candidates"][0]["leaves"][0]["leaf_id"] == episode_model.leaf_id(
        candidate_id, "note-a"
    )


def test_evidence_bounds_digest_only_and_revision_churn() -> None:
    from exomem import episode_model

    state = episode_model.start_episode("turn-42", {"digest": "a" * 64})
    assert state["input_revisions"][0]["recovery"] == "unavailable"
    with pytest.raises(episode_model.EpisodeError, match="EPISODE_REVISION_UNCHANGED"):
        episode_model.append_input_revision(state, {"digest": "a" * 64})
    with pytest.raises(episode_model.EpisodeError, match="EPISODE_EVIDENCE_INVALID"):
        episode_model.start_episode("turn-42", {"unknown": "x"})
    with pytest.raises(episode_model.EpisodeError, match="EPISODE_EVIDENCE_INVALID"):
        episode_model.start_episode("turn-42", {"excerpt": "x" * 9000})


def test_binding_checks_current_curation_operation_and_closed_proposal() -> None:
    from exomem import episode_model

    state, _candidate_id, _leaf_id, step = _prepared()
    assert state["candidates"][0]["leaves"][0]["binding"]["operation_id"]
    bad = _bound_plan(step)
    bad["operation_id"] = "0" * 64
    unbound = episode_model.start_episode("turn-42", _input())
    unbound = episode_model.declare_candidate(unbound, "other")
    candidate_id = unbound["candidates"][0]["candidate_id"]
    unbound = episode_model.revise_proposal(unbound, candidate_id, _proposal(step))
    unbound = episode_model.set_disposition(unbound, candidate_id, "routed", "Capture it.")
    leaf_id = unbound["candidates"][0]["leaves"][0]["leaf_id"]
    with pytest.raises(episode_model.EpisodeError, match="EPISODE_OPERATION_MISMATCH"):
        episode_model.bind_curation_leaf(unbound, candidate_id, leaf_id, bad)
    with pytest.raises(episode_model.EpisodeError, match="EPISODE_PROPOSAL_INVALID"):
        episode_model.revise_proposal(unbound, candidate_id, {**_proposal(step), "success": True})


def test_uncertain_attempt_blocks_retry_rebind_removal_and_change_until_reconciliation() -> None:
    from exomem import episode_model

    state, candidate_id, leaf_id, _step_value = _prepared()
    attempted = episode_model.mark_attempt_started(state, candidate_id, leaf_id)
    leaf = attempted["candidates"][0]["leaves"][0]
    assert leaf["outcome"] == "uncertain"
    with pytest.raises(episode_model.EpisodeError, match="EPISODE_ATTEMPT_UNCERTAIN"):
        episode_model.mark_attempt_started(attempted, candidate_id, leaf_id)
    with pytest.raises(episode_model.EpisodeError, match="EPISODE_ATTEMPT_UNCERTAIN"):
        episode_model.revise_proposal(attempted, candidate_id, _proposal(_step(suffix="changed")))
    observation = episode_model.VerifiedLeafOutcome(
        candidate_id,
        leaf_id,
        leaf["binding"]["operation_id"],
        leaf["effect_digest"],
        "proven_uncommitted",
        "b" * 64,
    )
    reconciled = episode_model.reconcile_leaf(attempted, observation)
    retried = episode_model.mark_attempt_started(reconciled, candidate_id, leaf_id)
    assert retried["candidates"][0]["leaves"][0]["attempts"] == 2
    with pytest.raises(episode_model.EpisodeError, match="EPISODE_OUTCOME_TYPE_REQUIRED"):
        episode_model.reconcile_leaf(retried, {"outcome": "committed"})


def test_committed_effect_cannot_escape_dedupe_under_new_candidate_key() -> None:
    from exomem import episode_model

    state, candidate_id, leaf_id, step = _prepared()
    state = episode_model.mark_attempt_started(state, candidate_id, leaf_id)
    leaf = state["candidates"][0]["leaves"][0]
    state = episode_model.reconcile_leaf(
        state,
        episode_model.VerifiedLeafOutcome(
            candidate_id,
            leaf_id,
            leaf["binding"]["operation_id"],
            leaf["effect_digest"],
            "committed",
            "c" * 64,
            "e" * 64,
            "f" * 64,
        ),
    )
    state = episode_model.declare_candidate(state, "same-effect-new-key")
    candidate = next(
        item for item in state["candidates"] if item["candidate_key"] == "same-effect-new-key"
    )
    with pytest.raises(episode_model.EpisodeError, match="EPISODE_DUPLICATE_EFFECT"):
        episode_model.revise_proposal(state, candidate["candidate_id"], _proposal(step))


def test_correction_invalidates_current_review_but_not_historical_review_or_input() -> None:
    from exomem import episode_model

    state, _candidate_id, _leaf_id, _step_value = _prepared()
    reviewed = episode_model.attest_precommit(state, 1)
    original = copy.deepcopy(reviewed)
    corrected = episode_model.append_input_revision(
        reviewed, _input(excerpt="Corrected observation.")
    )
    assert corrected["precommit_attestations"] == original["precommit_attestations"]
    assert corrected["current_precommit"] is None
    assert corrected["covered_through_input_revision"] is None
    assert reviewed == original


def test_postcommit_keeps_pending_separate_from_coverage_for_partial_multileaf_work() -> None:
    from exomem import episode_model

    state = episode_model.start_episode("turn-42", _input())
    state = episode_model.declare_candidate(state, "two-leaves")
    candidate_id = state["candidates"][0]["candidate_id"]
    first, second = _step(suffix="first"), _step(suffix="second")
    state = episode_model.revise_proposal(state, candidate_id, _proposal(first, second))
    state = episode_model.set_disposition(state, candidate_id, "routed", "Both effects are needed.")
    leaves = state["candidates"][0]["leaves"]
    state = episode_model.bind_curation_leaf(
        state, candidate_id, leaves[0]["leaf_id"], _bound_plan(first)
    )
    state = episode_model.bind_curation_leaf(
        state, candidate_id, leaves[1]["leaf_id"], _bound_plan(second)
    )
    state = episode_model.attest_precommit(state, 1)
    state = episode_model.mark_attempt_started(state, candidate_id, leaves[0]["leaf_id"])
    leaf = state["candidates"][0]["leaves"][0]
    state = episode_model.reconcile_leaf(
        state,
        episode_model.VerifiedLeafOutcome(
            candidate_id,
            leaf["leaf_id"],
            leaf["binding"]["operation_id"],
            leaf["effect_digest"],
            "committed",
            "d" * 64,
            "e" * 64,
            "f" * 64,
        ),
    )
    attested = episode_model.attest_postcommit(state, 1, [leaves[0]["leaf_id"]])
    assert attested["covered_through_input_revision"] is None
    assert attested["pending_leaf_ids"] == [leaves[1]["leaf_id"]]

    assert attested["reviewed_through_input_revision"] == 1
    assert attested["complete"] is False


def test_binding_accepts_a_real_curation_store_sealed_plan(vault) -> None:  # noqa: ANN001
    from exomem import curation, episode_model

    step = _step()
    plan = {
        "version": 1,
        "title": "Episode curation plan",
        "steps": [{"step_id": "write-note", "kind": step["kind"], "args": step["args"]}],
    }
    stored = curation.CurationStore(vault).create_forward(
        plan, binding_manifest=[], registry_ids={}
    )
    state = episode_model.start_episode("turn-42", _input())
    state = episode_model.declare_candidate(state, "observation")
    candidate_id = state["candidates"][0]["candidate_id"]
    state = episode_model.revise_proposal(state, candidate_id, _proposal(step))
    state = episode_model.set_disposition(state, candidate_id, "routed", "Capture it.")
    leaf_id = state["candidates"][0]["leaves"][0]["leaf_id"]
    binding = {
        "sealed_plan": stored["plan"],
        **{key: stored[key] for key in ("run_id", "plan_id", "plan_fingerprint")},
        "ordinal": 0,
        "step_id": "write-note",
        "operation_id": curation.operation_id(stored["plan_id"], 0, "write-note"),
    }
    bound = episode_model.bind_curation_leaf(state, candidate_id, leaf_id, binding)

    with pytest.raises(episode_model.EpisodeError, match="EPISODE_PRECOMMIT_REQUIRED"):
        episode_model.mark_attempt_started(bound, candidate_id, leaf_id)


def test_changed_unattempted_effect_requires_explicit_effect_revision() -> None:
    from exomem import episode_model

    state, candidate_id, _leaf_id, _step_value = _prepared()
    changed = _step(suffix="one")
    changed["args"] = {
        "title": "Changed",
        "content": chr(10).join(("## Observations", "", "- [finding] Changed.", "")),
    }
    with pytest.raises(episode_model.EpisodeError, match="EPISODE_EFFECT_REVISION_REQUIRED"):
        episode_model.revise_proposal(state, candidate_id, _proposal(changed))
    changed["effect_revision"] = 2
    revised = episode_model.revise_proposal(state, candidate_id, _proposal(changed))

    assert revised["candidates"][0]["leaves"][0]["effect_revision"] == 2


def test_reviewer_regressions_for_dispositions_duplicates_and_routes() -> None:
    from exomem import episode_model

    deferred, candidate_id, leaf_id, _step_value = _prepared()
    deferred = episode_model.set_disposition(
        deferred, candidate_id, "deferred", "Wait for the owning adapter."
    )
    deferred = episode_model.attest_precommit(deferred, 1)
    with pytest.raises(episode_model.EpisodeError, match="EPISODE_DISPOSITION_INVALID"):
        episode_model.mark_attempt_started(deferred, candidate_id, leaf_id)

    state = episode_model.start_episode("turn-duplicates", _input())
    state = episode_model.declare_candidate(state, "candidate")
    candidate_id = state["candidates"][0]["candidate_id"]
    same_a = _step(suffix="a")
    same_b = {**same_a, "leaf_key": "note-b"}
    with pytest.raises(episode_model.EpisodeError, match="EPISODE_DUPLICATE_EFFECT"):
        episode_model.revise_proposal(state, candidate_id, _proposal(same_a, same_b))

    state = episode_model.start_episode("turn-records", _input())
    state = episode_model.declare_candidate(state, "records-event")
    candidate_id = state["candidates"][0]["candidate_id"]
    pending = {**_proposal(), "route": "records", "leaves": []}
    state = episode_model.revise_proposal(state, candidate_id, pending)
    state = episode_model.set_disposition(state, candidate_id, "routed", "Await Records adapter.")
    state = episode_model.attest_precommit(state, 1)
    assert state["pending_leaf_ids"] == [candidate_id]


def test_uncertain_metadata_is_frozen_and_proven_uncommitted_effect_can_revise() -> None:
    from exomem import episode_model

    state, candidate_id, leaf_id, step = _prepared()
    attempted = episode_model.mark_attempt_started(state, candidate_id, leaf_id)
    with pytest.raises(episode_model.EpisodeError, match="EPISODE_ATTEMPT_UNCERTAIN"):
        episode_model.revise_proposal(
            attempted,
            candidate_id,
            {**_proposal(step), "reason": "Changed metadata after attempt."},
        )
    leaf = attempted["candidates"][0]["leaves"][0]
    recovered = episode_model.reconcile_leaf(
        attempted,
        episode_model.VerifiedLeafOutcome(
            candidate_id,
            leaf_id,
            leaf["binding"]["operation_id"],
            leaf["effect_digest"],
            "proven_uncommitted",
            "a" * 64,
        ),
    )
    revised = _step()
    revised["effect_revision"] = 2
    revised["args"] = {
        "title": "Corrected note",
        "content": "## Observations\n\n- [finding] Corrected.\n",
    }
    revised_state = episode_model.revise_proposal(recovered, candidate_id, _proposal(revised))
    revised_leaf = revised_state["candidates"][0]["leaves"][0]
    assert revised_leaf["binding"] is None
    assert revised_leaf["effect_history"][0]["outcome_proof"]["receipt_digest"] == "a" * 64


def test_binding_rejects_invalid_run_and_postcommit_duplicates() -> None:
    from exomem import episode_model

    state, candidate_id, leaf_id, step = _prepared()
    binding = _bound_plan(step)
    binding["run_id"] = "not-a-curation-run"
    unbound = episode_model.start_episode("turn-run", _input())
    unbound = episode_model.declare_candidate(unbound, "candidate")
    other = unbound["candidates"][0]["candidate_id"]
    unbound = episode_model.revise_proposal(unbound, other, _proposal(step))
    unbound = episode_model.set_disposition(unbound, other, "routed", "Capture it.")
    other_leaf = unbound["candidates"][0]["leaves"][0]["leaf_id"]
    with pytest.raises(episode_model.EpisodeError, match="EPISODE_BINDING_INVALID"):
        episode_model.bind_curation_leaf(unbound, other, other_leaf, binding)

    state = episode_model.mark_attempt_started(state, candidate_id, leaf_id)
    leaf = state["candidates"][0]["leaves"][0]
    state = episode_model.reconcile_leaf(
        state,
        episode_model.VerifiedLeafOutcome(
            candidate_id,
            leaf_id,
            leaf["binding"]["operation_id"],
            leaf["effect_digest"],
            "committed",
            "b" * 64,
            "c" * 64,
            "d" * 64,
        ),
    )
    with pytest.raises(episode_model.EpisodeError, match="EPISODE_POSTCOMMIT_INVALID"):
        episode_model.attest_postcommit(state, 1, [leaf_id, leaf_id])


def test_digest_only_input_cannot_be_precommitted_or_completed() -> None:
    from exomem import episode_model

    state = episode_model.start_episode("turn-digest", {"digest": "a" * 64})
    with pytest.raises(episode_model.EpisodeError, match="EPISODE_RECOVERY_UNAVAILABLE"):
        episode_model.attest_precommit(state, 1)


def test_binding_preserves_entity_candidate_from_real_curation_plan(vault) -> None:  # noqa: ANN001
    from exomem import curation, episode_model

    args = {
        "entity_type": "organization",
        "name": "amber guild",
        "summary": "A stable identity supported by reusable contexts.",
    }
    entity_candidate = {
        "review_ref": "exomem://review/" + "a" * 24,
        "review_fingerprint": "b" * 24,
        "candidate_state": "promotion",
        "identity": "amber guild",
        "signal_version": "c" * 16,
        "first_disconnected_context_batch": [],
        "remaining_disconnected_count": 0,
        "batch_fingerprint": None,
        "target_refs": [],
        "grammar_identity": {"version": "identity-v1", "predicate_table_digest": None},
        "registry_identity": "d" * 64,
    }
    plan = {
        "version": 1,
        "title": "Promote entity",
        "entity_candidate": entity_candidate,
        "steps": [{"step_id": "promote", "kind": "create-entity", "args": args}],
    }
    stored = curation.CurationStore(vault).create_forward(
        plan, binding_manifest=[], registry_ids={}
    )
    leaf = {
        "leaf_key": "promote",
        "effect_revision": 1,
        "kind": "create-entity",
        "args": args,
    }
    state = episode_model.start_episode("turn-entity", _input())
    state = episode_model.declare_candidate(state, "amber-guild")
    candidate_id = state["candidates"][0]["candidate_id"]
    state = episode_model.revise_proposal(state, candidate_id, _proposal(leaf))
    state = episode_model.set_disposition(state, candidate_id, "routed", "Promote it.")
    leaf_id = state["candidates"][0]["leaves"][0]["leaf_id"]
    binding = {
        "sealed_plan": stored["plan"],
        **{key: stored[key] for key in ("run_id", "plan_id", "plan_fingerprint")},
        "ordinal": 0,
        "step_id": "promote",
        "operation_id": curation.operation_id(stored["plan_id"], 0, "promote"),
    }

    bound = episode_model.bind_curation_leaf(state, candidate_id, leaf_id, binding)

    assert bound["candidates"][0]["leaves"][0]["binding"]["plan_id"] == stored["plan_id"]


def test_no_capture_route_has_no_effect_and_cannot_be_routed() -> None:
    from exomem import episode_model as model

    state = model.declare_candidate(model.start_episode("abstain", _input()), "incidental")
    candidate = state["candidates"][0]["candidate_id"]
    with pytest.raises(model.EpisodeError, match="EPISODE_PROPOSAL_INVALID"):
        model.revise_proposal(state, candidate, {**_proposal(), "route": "no_capture"})
    state = model.revise_proposal(
        state, candidate, {**_proposal(), "route": "no_capture", "leaves": []}
    )
    with pytest.raises(model.EpisodeError, match="EPISODE_DISPOSITION_INVALID"):
        model.set_disposition(state, candidate, "routed", "Cannot execute an abstention.")
    state = model.set_disposition(state, candidate, "no_capture", "No durable change.")
    state = model.attest_precommit(state, 1)
    assert model.attest_postcommit(state, 1, [])["complete"] is True


def test_routed_disposition_requires_a_proposal() -> None:
    from exomem import episode_model as model

    state = model.declare_candidate(model.start_episode("unproposed", _input()), "candidate")
    with pytest.raises(model.EpisodeError, match="EPISODE_DISPOSITION_INVALID"):
        model.set_disposition(state, state["candidates"][0]["candidate_id"], "routed", "Not ready.")


def test_new_effect_revision_starts_at_one() -> None:
    from exomem import episode_model as model

    state = model.declare_candidate(model.start_episode("new-effect", _input()), "candidate")
    with pytest.raises(model.EpisodeError, match="EPISODE_EFFECT_REVISION_REQUIRED"):
        model.revise_proposal(
            state,
            state["candidates"][0]["candidate_id"],
            _proposal({**_step(), "effect_revision": 2}),
        )


def _proven_uncommitted(state, candidate, leaf_id, receipt):
    from exomem import episode_model as model

    state = model.mark_attempt_started(state, candidate, leaf_id)
    leaf = state["candidates"][0]["leaves"][0]
    return model.reconcile_leaf(
        state,
        model.VerifiedLeafOutcome(
            candidate,
            leaf_id,
            leaf["binding"]["operation_id"],
            leaf["effect_digest"],
            "proven_uncommitted",
            receipt,
        ),
    )


def _changed_effect(step):
    return {**step, "effect_revision": 2, "args": {**step["args"], "title": "Revised outcome"}}


def test_retry_history_survives_new_attempt_and_effect_revision(monkeypatch) -> None:
    from exomem import episode_model as model

    state, candidate, leaf_id, step = _prepared()
    original_binding = copy.deepcopy(state["candidates"][0]["leaves"][0]["binding"])
    for receipt in ("a" * 64, "b" * 64):
        state = _proven_uncommitted(state, candidate, leaf_id, receipt)
    history = state["candidates"][0]["leaves"][0]["attempt_history"]
    assert [item["outcome_proof"]["receipt_digest"] for item in history] == ["a" * 64, "b" * 64]
    pending = model.mark_attempt_started(state, candidate, leaf_id)
    assert pending["candidates"][0]["leaves"][0]["attempt_history"][-1]["outcome"] == "uncertain"
    assert pending["candidates"][0]["leaves"][0]["attempt_history"][:2] == history
    revised = model.revise_proposal(state, candidate, _proposal(_changed_effect(step)))
    archived = revised["candidates"][0]["leaves"][0]["effect_history"][0]
    assert archived["attempts"] == 2
    assert archived["binding"] == original_binding
    assert archived["attempt_history"] == history
    revised["candidates"][0]["leaves"][0]["effect_history"][0]["attempt_history"][0]["outcome"] = (
        "changed"
    )
    assert history[0]["outcome"] == "proven_uncommitted"
    monkeypatch.setattr(model, "MAX_ATTEMPTS", 2)
    with pytest.raises(model.EpisodeError, match="EPISODE_TOO_LARGE"):
        model.mark_attempt_started(state, candidate, leaf_id)


def test_historical_effect_cannot_rebind_or_disappear_after_revision() -> None:
    from exomem import episode_model as model

    state, candidate, leaf_id, step = _prepared()
    state = _proven_uncommitted(state, candidate, leaf_id, "a" * 64)
    state = model.revise_proposal(state, candidate, _proposal(_changed_effect(step)))
    with pytest.raises(model.EpisodeError, match="EPISODE_DUPLICATE_EFFECT"):
        model.revise_proposal(state, candidate, _proposal({**step, "effect_revision": 3}))
    with pytest.raises(model.EpisodeError, match="EPISODE_ATTEMPTED_LEAF"):
        model.revise_proposal(state, candidate, _proposal(_step(suffix="replacement")))
