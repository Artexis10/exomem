"""The no-nudge observation record (close-memory-loop task 1.4).

One record separates five things a capture-to-activation run produces:
how the host initiated the turn, what the agent decided, which leaf effects
landed, whether publication was current, and whether a later fresh-session
response was useful. The contract these tests pin is the one the task's
verify clause names: **a forced-call test cannot satisfy ordinary-agent
initiation acceptance**, however perfect its effects, publication and later
answer are. Forced plumbing is still reported, under its own verdict, so a
scripted run proves transport without ever being counted as initiation.
"""

from __future__ import annotations

import pytest
from epistemic.memory_loop import observation as obs
from pydantic import ValidationError

TURNS = ("We moved the rye order to the mill on the ridge; the sack says July milling.",)
TURNS_SHA = obs.turns_sha256(TURNS)
DIGEST = "a" * 64
PROMPTS = {
    "server_instructions": "Before a substantive turn, call activate_context once.",
    "activation_hook": "Pre-turn packet from the installed hook.",
    "stop_hook_checkpoint": "Record a recap at the next stopping point.",
}
FROZEN = obs.Frozen(
    fixture_id="memory-loop-test-v1",
    actor_sha256=DIGEST,
    pre_capture_sha256=DIGEST,
    evaluator_sha256=DIGEST,
    turns_sha256=TURNS_SHA,
    later_turn_sha256=DIGEST,
    shipped_prompt_sha256=frozenset(obs.text_sha256(text) for text in PROMPTS.values()),
)


def _prompt(kind: str) -> dict:
    return {"kind": kind, "detail": kind.replace("_", " "), "sha256": obs.text_sha256(PROMPTS[kind])}


def _decision(seq: int, phase: str, **fields) -> dict:
    base = {"seq": seq, "phase": phase, "initiator": "agent"}
    base.update(fields)
    return base


def _effect(seq: int, candidate: str = "rye", **fields) -> dict:
    base = {
        "seq": seq,
        "candidate_key": candidate,
        "initiator": "agent",
        "kind": "create_note",
        "path": "Knowledge Base/Notes/Insights/rye.md",
        "outcome": "committed",
        "receipt_sha256": "b" * 64,
    }
    base.update(fields)
    return base


def _record(**overrides) -> dict:
    record = {
        "artifact_type": obs.ARTIFACT_TYPE,
        "schema_version": 1,
        "fixture_id": "memory-loop-test-v1",
        "actor_sha256": DIGEST,
        "pre_capture_sha256": DIGEST,
        "evaluator_sha256": DIGEST,
        "host_initiation": {
            "client": {"client": "generic-mcp", "adapter_version": "0.0-test", "lifecycle": "best_effort"},
            "input_origin": "synthetic_fixture",
            "delivered_turns_sha256": TURNS_SHA,
            "user_reminders": [],
            "product_prompts": [_prompt("server_instructions")],
            "harness_interventions": [],
        },
        "agent_decisions": [
            _decision(1, "activation"),
            _decision(
                2,
                "decomposition",
                candidate_key="rye",
                partition={
                    "partition_id": "p1",
                    "retrieval_question": "what do we know about the rye",
                    "subject": "flour",
                    "temporal_episode": "2026-09",
                    "epistemic_role": "direct observation",
                },
            ),
            _decision(3, "destination", candidate_key="rye", route="focused_note", title="Ridge rye"),
            _decision(4, "disposition", candidate_key="rye", disposition="routed", reason="distinct question"),
            _decision(5, "precommit_review"),
        ],
        "leaf_effects": [_effect(10)],
        "publication": {"status": "current", "seq": 20},
        "later_response": {
            "seq": 30,
            "fresh_session": True,
            "turn_sha256": DIGEST,
            "activation_initiator": "agent",
            "reminder_turns": [],
            "response_text": "The rye was milled in July.",
            "usefulness_grade": "useful",
            "grader": "blind_rubric",
        },
    }
    record.update(overrides)
    return record


def _load(**overrides) -> obs.NoNudgeObservation:
    return obs.load_observation(_record(**overrides))


def _with_initiation(**fields) -> dict:
    record = _record()
    record["host_initiation"] = {**record["host_initiation"], **fields}
    return record


# --------------------------------------------------------------------------- #
# The ordinary record passes every verdict (the positive control)
# --------------------------------------------------------------------------- #


def test_an_ordinary_record_passes_ordinary_agent_acceptance() -> None:
    report = obs.evaluate(_load(), FROZEN)

    assert report.ordinary_initiation.outcome == "pass", report.ordinary_initiation.reasons
    assert report.leaf_effects.outcome == "pass"
    assert report.publication.outcome == "pass"
    assert report.later_usefulness.outcome == "pass"
    assert report.ordinary_agent_acceptance.outcome == "pass"


def test_the_report_keeps_five_separate_verdicts_and_no_score() -> None:
    report = obs.evaluate(_load(), FROZEN)
    verdicts = obs.report_to_dict(report)["verdicts"]

    assert set(verdicts) == {
        "ordinary_initiation",
        "agent_decisions",
        "leaf_effects",
        "publication",
        "later_usefulness",
        "exact_private_replay",
        "ordinary_agent_acceptance",
    }
    assert all(set(item) == {"outcome", "reasons"} for item in verdicts.values())
    assert "score" not in obs.report_to_dict(report)


def test_initiation_class_is_reported_from_the_client_lifecycle_not_its_brand() -> None:
    best_effort = obs.evaluate(_load(), FROZEN)
    assert best_effort.initiation_class == "best_effort"

    enforced = _with_initiation(
        client={"client": "generic-mcp", "adapter_version": "0.0-test", "lifecycle": "lifecycle_enforced"},
        product_prompts=[_prompt("activation_hook")],
    )
    report = obs.evaluate(obs.load_observation(enforced), FROZEN)
    assert report.initiation_class == "lifecycle_enforced"
    assert report.ordinary_initiation.outcome == "pass"


def test_a_product_hook_may_initiate_activation_on_a_lifecycle_client() -> None:
    record = _with_initiation(
        client={"client": "hooked", "adapter_version": "1", "lifecycle": "lifecycle_enforced"},
        product_prompts=[_prompt("activation_hook")],
    )
    record["agent_decisions"][0]["initiator"] = "product_hook"

    report = obs.evaluate(obs.load_observation(record), FROZEN)

    assert report.ordinary_initiation.outcome == "pass", report.ordinary_initiation.reasons


# --------------------------------------------------------------------------- #
# Forced calls never satisfy ordinary-agent initiation (the verify clause)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("kind", obs.HARNESS_INTERVENTION_KINDS)
def test_any_harness_intervention_fails_initiation_but_not_transport(kind: str) -> None:
    record = _with_initiation(harness_interventions=[{"kind": kind, "detail": "benchmark harness"}])

    report = obs.evaluate(obs.load_observation(record), FROZEN)

    assert report.ordinary_initiation.outcome == "fail"
    assert any(kind in reason for reason in report.ordinary_initiation.reasons)
    # The plumbing still worked and is reported as such, separately.
    assert report.leaf_effects.outcome == "pass"
    assert report.ordinary_agent_acceptance.outcome == "fail"


def test_a_scripted_write_without_an_agent_decision_fails_initiation() -> None:
    record = _record(agent_decisions=[_decision(1, "activation")])
    record["leaf_effects"] = [_effect(10, initiator="harness")]

    report = obs.evaluate(obs.load_observation(record), FROZEN)

    assert report.ordinary_initiation.outcome == "fail"
    assert report.agent_decisions.outcome == "fail"
    assert report.leaf_effects.outcome == "pass"


def test_an_agent_executed_effect_needs_a_prior_agent_routed_disposition() -> None:
    record = _record()
    # The disposition arrives after the effect: the effect was not decided first.
    record["agent_decisions"] = [
        item for item in record["agent_decisions"] if item["phase"] != "disposition"
    ] + [_decision(15, "disposition", candidate_key="rye", disposition="routed", reason="late")]

    report = obs.evaluate(obs.load_observation(record), FROZEN)

    assert report.agent_decisions.outcome == "fail"
    assert any("rye" in reason for reason in report.agent_decisions.reasons)


@pytest.mark.parametrize("initiator", ["harness", "user"])
@pytest.mark.parametrize("phase", ["decomposition", "destination", "disposition", "precommit_review"])
def test_a_decision_the_agent_did_not_initiate_fails_initiation(initiator: str, phase: str) -> None:
    record = _record()
    for item in record["agent_decisions"]:
        if item["phase"] == phase:
            item["initiator"] = initiator

    report = obs.evaluate(obs.load_observation(record), FROZEN)

    assert report.ordinary_initiation.outcome == "fail"


def test_a_product_hook_never_authors_a_semantic_decision() -> None:
    record = _with_initiation(
        client={"client": "hooked", "adapter_version": "1", "lifecycle": "lifecycle_enforced"},
        product_prompts=[_prompt("stop_hook_checkpoint")],
    )
    for item in record["agent_decisions"]:
        if item["phase"] == "destination":
            item["initiator"] = "product_hook"

    report = obs.evaluate(obs.load_observation(record), FROZEN)

    assert report.ordinary_initiation.outcome == "fail"


def test_a_user_reminder_fails_initiation() -> None:
    record = _with_initiation(user_reminders=["can you save that to memory?"])

    report = obs.evaluate(obs.load_observation(record), FROZEN)

    assert report.ordinary_initiation.outcome == "fail"
    assert report.ordinary_agent_acceptance.outcome == "fail"


def test_turns_other_than_the_fixture_input_fail_initiation() -> None:
    nudged = obs.turns_sha256((*TURNS, "Please save this and link it."))
    record = _with_initiation(delivered_turns_sha256=nudged)

    report = obs.evaluate(obs.load_observation(record), FROZEN)

    assert report.ordinary_initiation.outcome == "fail"
    assert any("delivered turns" in reason for reason in report.ordinary_initiation.reasons)


def test_a_record_where_the_agent_did_nothing_is_not_ordinary_initiation() -> None:
    record = _record(agent_decisions=[], leaf_effects=[])

    report = obs.evaluate(obs.load_observation(record), FROZEN)

    assert report.ordinary_initiation.outcome == "fail"


def test_every_forced_variant_of_a_perfect_record_fails_acceptance() -> None:
    """Exhaustive over the ways a harness can reach a correct end state."""

    variants: list[dict] = []
    for kind in obs.HARNESS_INTERVENTION_KINDS:
        variants.append(_with_initiation(harness_interventions=[{"kind": kind, "detail": "x"}]))
    scripted = _record()
    for effect in scripted["leaf_effects"]:
        effect["initiator"] = "harness"
    variants.append(scripted)
    forced_later = _record()
    forced_later["later_response"] = {**forced_later["later_response"], "activation_initiator": "harness"}
    variants.append(forced_later)
    reminded_later = _record()
    reminded_later["later_response"] = {
        **reminded_later["later_response"],
        "reminder_turns": ["remember what we said about the rye?"],
    }
    variants.append(reminded_later)

    for variant in variants:
        report = obs.evaluate(obs.load_observation(variant), FROZEN)
        assert report.ordinary_agent_acceptance.outcome == "fail", variant


# --------------------------------------------------------------------------- #
# Publication and the later response are measured separately
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("status", ["pending", "warming", "unavailable"])
def test_non_current_publication_fails_honestly(status: str) -> None:
    report = obs.evaluate(_load(publication={"status": status, "seq": 20}), FROZEN)

    assert report.publication.outcome == "fail"
    assert report.ordinary_initiation.outcome == "pass"


def test_unobserved_publication_is_unmeasured_not_passed() -> None:
    report = obs.evaluate(
        _load(publication={"status": "not_observed", "seq": None}), FROZEN
    )

    assert report.publication.outcome == "unmeasured"
    assert report.ordinary_agent_acceptance.outcome != "pass"


def test_publication_observed_before_the_last_effect_is_not_current_for_it() -> None:
    record = _record(publication={"status": "current", "seq": 8})

    with pytest.raises(ValidationError):
        obs.load_observation(record)


def test_a_later_response_that_is_not_a_fresh_session_fails_usefulness() -> None:
    record = _record()
    record["later_response"] = {**record["later_response"], "fresh_session": False}

    report = obs.evaluate(obs.load_observation(record), FROZEN)

    assert report.later_usefulness.outcome == "fail"


def test_an_ungraded_later_response_is_unmeasured() -> None:
    record = _record()
    record["later_response"] = {
        **record["later_response"],
        "usefulness_grade": "ungraded",
        "grader": "none",
    }

    report = obs.evaluate(obs.load_observation(record), FROZEN)

    assert report.later_usefulness.outcome == "unmeasured"


def test_a_missing_later_response_is_unmeasured() -> None:
    report = obs.evaluate(_load(later_response=None), FROZEN)

    assert report.later_usefulness.outcome == "unmeasured"
    assert report.ordinary_agent_acceptance.outcome != "pass"


# --------------------------------------------------------------------------- #
# Exact private replay is its own verdict
# --------------------------------------------------------------------------- #


def test_a_synthetic_fixture_run_is_not_an_exact_private_replay() -> None:
    report = obs.evaluate(_load(), FROZEN)

    assert report.exact_private_replay.outcome == "not_applicable"


def test_a_reconstructed_input_leaves_exact_replay_unmeasured() -> None:
    record = _with_initiation(input_origin="reconstructed")

    report = obs.evaluate(obs.load_observation(record), FROZEN)

    assert report.exact_private_replay.outcome == "unmeasured"
    assert any("reconstruct" in reason for reason in report.exact_private_replay.reasons)


def test_an_original_private_input_without_bound_digests_is_unmeasured() -> None:
    record = _with_initiation(input_origin="original_private")

    report = obs.evaluate(obs.load_observation(record), FROZEN)

    assert report.exact_private_replay.outcome == "unmeasured"


def test_an_original_private_input_bound_to_its_digests_is_an_exact_replay() -> None:
    private = obs.Frozen(
        **{**FROZEN.__dict__, "private_input_sha256": "c" * 64, "private_snapshot_sha256": "d" * 64}
    )
    record = _with_initiation(input_origin="original_private")
    record["private_binding"] = {"original_input_sha256": "c" * 64, "snapshot_sha256": "d" * 64}

    report = obs.evaluate(obs.load_observation(record), private)

    assert report.exact_private_replay.outcome == "pass"
    assert obs.void_reasons(obs.load_observation(record), private) == ()


# --------------------------------------------------------------------------- #
# Structural validation of the record
# --------------------------------------------------------------------------- #


def test_a_best_effort_client_cannot_carry_a_lifecycle_hook_prompt() -> None:
    record = _with_initiation(product_prompts=[_prompt("activation_hook")])

    with pytest.raises(ValidationError, match="lifecycle"):
        obs.load_observation(record)


def test_sequence_numbers_are_unique_across_the_record() -> None:
    record = _record()
    record["leaf_effects"] = [_effect(4)]

    with pytest.raises(ValidationError, match="seq"):
        obs.load_observation(record)


def test_the_later_response_comes_after_every_effect() -> None:
    record = _record()
    record["later_response"] = {**record["later_response"], "seq": 9}
    record["publication"] = {"status": "current", "seq": 8}

    with pytest.raises(ValidationError):
        obs.load_observation(record)


def test_a_decomposition_decision_carries_all_four_partition_axes() -> None:
    record = _record()
    record["agent_decisions"][1]["partition"] = {
        **record["agent_decisions"][1]["partition"],
        "temporal_episode": "",
    }

    with pytest.raises(ValidationError):
        obs.load_observation(record)


def test_a_destination_names_exactly_one_existing_target_or_new_title() -> None:
    record = _record()
    record["agent_decisions"][2]["target"] = "Knowledge Base/Notes/x.md"

    with pytest.raises(ValidationError, match="target"):
        obs.load_observation(record)


def test_unknown_fields_are_refused() -> None:
    record = _record()
    record["host_initiation"]["forced"] = False

    with pytest.raises(ValidationError):
        obs.load_observation(record)


def test_the_record_round_trips_through_json() -> None:
    loaded = _load()
    again = obs.load_observation_json(obs.dump_observation_json(loaded))

    assert again == loaded


# --------------------------------------------------------------------------- #
# Binding to the fixture's frozen digests, and the acceptance gate
# --------------------------------------------------------------------------- #


class _Check:
    def __init__(self, accepted: bool) -> None:
        self.accepted = accepted

    def failed(self) -> tuple[str, ...]:
        return () if self.accepted else ("some/expectation",)


def test_a_record_bound_to_the_frozen_digests_is_not_void() -> None:
    assert obs.void_reasons(_load(), FROZEN) == ()


@pytest.mark.parametrize("field", ["actor_sha256", "pre_capture_sha256", "evaluator_sha256"])
def test_a_record_bound_to_other_digests_is_void_not_rescored(field: str) -> None:
    reasons = obs.void_reasons(_load(**{field: "c" * 64}), FROZEN)

    assert len(reasons) == 1 and field in reasons[0]


def test_a_later_response_to_another_turn_is_void() -> None:
    record = _record()
    record["later_response"] = {**record["later_response"], "turn_sha256": "e" * 64}

    reasons = obs.void_reasons(obs.load_observation(record), FROZEN)

    assert reasons == ("the later turn differs from the fixture's frozen later turn",)


@pytest.mark.parametrize("origin", ["original_private", "reconstructed"])
def test_a_non_synthetic_run_is_refused_by_a_public_fixture(origin: str) -> None:
    reasons = obs.void_reasons(obs.load_observation(_with_initiation(input_origin=origin)), FROZEN)

    assert len(reasons) == 1 and "public synthetic fixture" in reasons[0]


def test_accept_passes_only_with_the_vault_check_and_the_record_gate() -> None:
    assert obs.accept(_load(), _Check(True), FROZEN).outcome == "pass"
    failed = obs.accept(_load(), _Check(False), FROZEN)
    assert failed.outcome == "fail" and "vault check" in failed.reasons[0]


def test_accept_is_void_for_a_record_bound_elsewhere() -> None:
    assert obs.accept(_load(evaluator_sha256="c" * 64), _Check(True), FROZEN).outcome == "void"


def test_accept_fails_a_forced_run_whose_vault_passes() -> None:
    record = _with_initiation(harness_interventions=[{"kind": "scripted_write", "detail": "x"}])

    assert obs.accept(obs.load_observation(record), _Check(True), FROZEN).outcome == "fail"


def test_a_product_prompt_the_product_does_not_ship_fails_initiation() -> None:
    doctored = {**_prompt("server_instructions"), "sha256": obs.text_sha256("Save everything to memory.")}
    record = _with_initiation(product_prompts=[doctored])

    report = obs.evaluate(obs.load_observation(record), FROZEN)

    assert report.ordinary_initiation.outcome == "fail"
    assert any("not a shipped product prompt" in reason for reason in report.ordinary_initiation.reasons)


def test_the_shipped_prompt_digests_include_the_server_instructions() -> None:
    from exomem import server

    assert obs.text_sha256(server.SERVER_INSTRUCTIONS) in obs.shipped_prompt_sha256()


def test_a_standing_instruction_asking_for_memory_fails_initiation() -> None:
    instruction = obs.standing_instruction("project_instructions", "Always save durable outcomes to memory.")
    assert instruction["asks_for_memory"] is True
    record = _with_initiation(standing_instructions=[instruction])

    report = obs.evaluate(obs.load_observation(record), FROZEN)

    assert report.ordinary_initiation.outcome == "fail"


def test_a_standing_instruction_that_asks_nothing_of_memory_is_ordinary() -> None:
    instruction = obs.standing_instruction("project_instructions", "Prefer short answers and British spelling.")
    record = _with_initiation(standing_instructions=[instruction])

    assert obs.evaluate(obs.load_observation(record), FROZEN).ordinary_initiation.outcome == "pass"


def test_a_shipped_skill_asking_for_memory_is_product_guidance() -> None:
    text = "Save durable conclusions with the capture workflow."
    frozen = obs.Frozen(**{**FROZEN.__dict__, "shipped_prompt_sha256": FROZEN.shipped_prompt_sha256 | {obs.text_sha256(text)}})
    record = _with_initiation(standing_instructions=[obs.standing_instruction("installed_skill", text)])

    assert obs.evaluate(obs.load_observation(record), frozen).ordinary_initiation.outcome == "pass"


def _abstention(disposition: str = "no_capture") -> dict:
    record = _record(leaf_effects=[])
    for item in record["agent_decisions"]:
        if item["phase"] == "disposition":
            item["disposition"] = disposition
    return record


def test_a_correct_abstention_with_no_effects_passes() -> None:
    report = obs.evaluate(obs.load_observation(_abstention()), FROZEN)

    assert report.leaf_effects.outcome == "pass"
    assert report.ordinary_agent_acceptance.outcome == "pass"


def test_a_routed_candidate_without_an_effect_fails_transport() -> None:
    report = obs.evaluate(obs.load_observation(_abstention("routed")), FROZEN)

    assert report.leaf_effects.outcome == "fail"
