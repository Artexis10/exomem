"""ActionWorld: common tools, isolation, grading and damage accounting."""

from __future__ import annotations

from membench.utility.action_world import OTHER_PROJECT_MODIFIED, ActionWorld
from membench.utility.schema import AttemptStatus
from membench.utility.scenarios import generate_episode


def _world(seed: int = 1, variant: str = "helpful_history", arm: str = "memory") -> ActionWorld:
    episode = generate_episode(seed, variant)
    return ActionWorld(episode, arm=arm)


def test_tool_schemas_are_mcp_shaped() -> None:
    world = _world()
    schemas = world.tool_schemas()
    assert set(schemas) == {
        "list_projects",
        "inspect_project",
        "read_note",
        "write_note",
        "apply_config",
    }
    for schema in schemas.values():
        assert set(schema) == {"description", "inputSchema"}
        assert schema["inputSchema"]["type"] == "object"


def test_current_target_oracle_can_satisfy_world() -> None:
    """Submitting exactly the oracle's current state succeeds."""

    episode = generate_episode(2, "helpful_history")
    world = ActionWorld(episode, arm="memory")
    world.advance_phase(2)
    oracle = episode.oracle
    result = world.call(
        "apply_config",
        {
            "project": oracle.target_project,
            "steps": list(oracle.current_state.steps),
            "constraint": oracle.current_state.constraint,
        },
    )
    assert result["accepted"] is True
    outcome = world.grade()
    assert outcome.status is AttemptStatus.VALID
    assert outcome.success is True
    assert outcome.destructive_effects == ()


def test_an_earlier_write_cannot_replace_the_final_action():
    episode = generate_episode(2, "helpful_history")
    world = ActionWorld(episode)
    target = episode.oracle.current_state
    world.call("apply_config", {"project": target.project, "steps": list(target.steps), "constraint": target.constraint})
    world.advance_phase(2)
    assert world.grade().success is False


def test_reapplying_the_other_projects_existing_state_is_not_damage():
    episode = generate_episode(2, "helpful_history")
    world = ActionWorld(episode)
    world.advance_phase(2)
    for state in (episode.oracle.other_state, episode.oracle.current_state):
        world.call("apply_config", {"project": state.project, "steps": list(state.steps), "constraint": state.constraint})
    assert world.grade().success is True
    assert world.grade().destructive_effects == ()


def test_stale_distractor_current_facts_are_recoverable_via_tool() -> None:
    """Even though the history is stale, inspect_project always reports the
    live authoritative state once the change phase has been entered."""

    episode = generate_episode(2, "stale_distractor")
    world = ActionWorld(episode, arm="memory")
    world.advance_phase(1)  # enter the "change" phase
    oracle = episode.oracle
    seen = world.call("inspect_project", {"project": oracle.target_project})
    assert tuple(seen["steps"]) == oracle.current_state.steps
    assert seen["constraint"] == oracle.current_state.constraint
    # And it must differ from the stale initial state, or the test proves
    # nothing about staleness.
    assert oracle.initial_state != oracle.current_state


def test_no_action_fails() -> None:
    world = _world()
    outcome = world.grade()
    assert outcome.success is False
    assert outcome.failure_reason == "no_action"


def test_wrong_order_fails() -> None:
    episode = generate_episode(2, "self_contained")
    world = ActionWorld(episode, arm="memory")
    oracle = episode.oracle
    reversed_steps = list(reversed(oracle.current_state.steps))
    world.call(
        "apply_config",
        {"project": oracle.target_project, "steps": reversed_steps, "constraint": oracle.current_state.constraint},
    )
    outcome = world.grade()
    assert outcome.success is False
    assert outcome.failure_reason == "state_mismatch"


def test_wrong_project_records_actual_side_effect_not_just_refusal() -> None:
    episode = generate_episode(2, "helpful_history")
    world = ActionWorld(episode, arm="memory")
    oracle = episode.oracle
    result = world.call(
        "apply_config",
        {
            "project": oracle.other_project,
            "steps": list(oracle.current_state.steps),
            "constraint": oracle.current_state.constraint,
        },
    )
    assert result["accepted"] is True
    snap = world.snapshot()
    assert oracle.other_project in snap["applied"]
    outcome = world.grade()
    assert outcome.success is False
    assert OTHER_PROJECT_MODIFIED in outcome.destructive_effects


def test_destructive_effect_persists_even_after_later_repair() -> None:
    episode = generate_episode(2, "helpful_history")
    world = ActionWorld(episode, arm="memory")
    oracle = episode.oracle
    world.call(
        "apply_config",
        {"project": oracle.other_project, "steps": ["x", "y"], "constraint": "z"},
    )
    world.call(
        "apply_config",
        {
            "project": oracle.target_project,
            "steps": list(oracle.current_state.steps),
            "constraint": oracle.current_state.constraint,
        },
    )
    outcome = world.grade()
    # A correct final target write does not erase an earlier other-project
    # write: destructive side effects override an otherwise-correct outcome.
    assert outcome.success is False
    assert OTHER_PROJECT_MODIFIED in outcome.destructive_effects


def test_invalid_payload_is_a_bounded_error_not_an_exception() -> None:
    world = _world()
    result = world.call("apply_config", {"project": "x"})
    assert result == {"error": "invalid_payload"}
    result = world.call("apply_config", "not-a-dict")  # type: ignore[arg-type]
    assert result == {"error": "invalid_payload"}
    result = world.call("nonexistent_tool", {})
    assert result == {"error": "unknown_tool"}


def test_unknown_project_is_bounded_error() -> None:
    world = _world()
    result = world.call("inspect_project", {"project": "totally-unknown-project"})
    assert result == {"error": "unknown_project"}


def test_workspace_notes_survive_phase_transitions() -> None:
    world = _world()
    world.call("write_note", {"key": "k", "value": "v"})
    world.advance_phase(1)
    world.advance_phase(2)
    result = world.call("read_note", {"key": "k"})
    assert result["value"] == "v"


def test_notes_mapping_is_bounded() -> None:
    world = _world()
    for i in range(200):
        world.call("write_note", {"key": f"k{i}", "value": "v"})
    snap = world.snapshot()
    assert len(snap["notes"]) <= 32


def test_actor_visible_call_results_never_include_oracle_data() -> None:
    episode = generate_episode(4, "stale_distractor")
    world = ActionWorld(episode, arm="memory")
    result = world.call("list_projects", {})
    for value in result["projects"]:
        assert isinstance(value, str)
    result = world.call("inspect_project", {"project": episode.oracle.target_project})
    assert set(result) == {"project", "steps", "constraint"}


def test_common_tools_are_identical_for_both_arms() -> None:
    episode = generate_episode(1, "helpful_history")
    control_world = ActionWorld(episode, arm="control")
    memory_world = ActionWorld(episode, arm="memory")
    assert control_world.tool_schemas() == memory_world.tool_schemas()


def test_correct_target_with_other_project_damage_is_not_success() -> None:

    episode = generate_episode(1, "self_contained")
    world = _world(1, "self_contained", "memory")
    target = episode.oracle.target_project
    other = episode.oracle.other_project
    world.call(
        "apply_config",
        {"project": other, "steps": ["x"], "constraint": "y"},
    )
    current = episode.oracle.current_state
    world.call(
        "apply_config",
        {"project": target, "steps": list(current.steps), "constraint": current.constraint},
    )
    outcome = world.grade()
    assert outcome.success is False
    assert OTHER_PROJECT_MODIFIED in outcome.destructive_effects


def test_grade_snapshot_matches_live_grade_and_needs_no_world_instance() -> None:
    from membench.utility.action_world import grade_snapshot

    episode = generate_episode(2, "helpful_history")
    world = _world(2, "helpful_history", "memory")
    current = episode.oracle.current_state
    world.call("apply_config", {"project": current.project, "steps": list(current.steps), "constraint": current.constraint})
    live = world.grade()
    snapshot = world.snapshot()
    replayed = grade_snapshot(snapshot, episode.oracle, episode.episode_id, episode.variant, "memory")
    assert replayed == live


import pytest

# --------------------------------------------------------------------------
# Malformed or absent saved evidence is unsupported, never a clean pass.
# --------------------------------------------------------------------------

def _oracle():
    from membench.utility.scenarios import generate_episode

    return generate_episode(seed=3, variant="helpful_history").oracle


@pytest.mark.parametrize("snapshot", [
    {},
    {"applied": {}},
    {"write_events": []},
    {"applied": {}, "write_events": None},
    {"applied": None, "write_events": []},
    {"applied": {}, "write_events": [{"project": "X"}]},
    {"applied": {}, "write_events": [{"accepted": True}]},
])
def test_incomplete_saved_evidence_is_invalid_not_a_pass(snapshot):
    from membench.utility.action_world import grade_snapshot
    from membench.utility.schema import AttemptStatus

    outcome = grade_snapshot(snapshot, _oracle(), "EPI-x", "helpful_history", "control")
    assert outcome.status is AttemptStatus.INVALID
    assert outcome.success is not True
    assert outcome.failure_reason == "unsupported_evidence"


def test_correct_saved_evidence_still_grades_normally():
    from membench.utility.action_world import grade_snapshot
    from membench.utility.schema import AttemptStatus

    oracle = _oracle()
    snapshot = {
        "applied": {oracle.target_project: {"steps": list(oracle.current_state.steps),
                                            "constraint": oracle.current_state.constraint}},
        "write_events": [{"phase": 2, "project": oracle.target_project,
                          "steps": list(oracle.current_state.steps),
                          "constraint": oracle.current_state.constraint, "accepted": True}],
    }
    outcome = grade_snapshot(snapshot, oracle, "EPI-x", "helpful_history", "control")
    assert outcome.status is AttemptStatus.VALID and outcome.success is True


def test_runner_evidence_matches_the_registered_epistemic_predicates():
    """The registered wrappers and the runner read the same observed state."""
    from epistemic.assertions import AssertionContext, utility_action_state_valid, utility_no_prohibited_effects
    from epistemic.snapshot import EpistemicStateSnapshot
    from membench.utility.action_world import ActionWorld
    from membench.utility.scenarios import generate_episode

    def context(observed):
        return AssertionContext(snapshot=EpistemicStateSnapshot(
            provider="fixture", variant="utility", phase="action",
            taken_at="2026-09-13T00:00:00Z", items=(), relations=(), declarations=(),
            projector={"name": "utility", "version": "1", "author": "fixture",
                       "endpoints_used": (), "loc": 0}),
            utility_world_snapshot=observed, utility_oracle=episode.oracle)

    episode = generate_episode(seed=3, variant="helpful_history")
    world = ActionWorld(episode, arm="memory")
    world.advance_phase(2)
    world.call("apply_config", {"project": episode.oracle.target_project,
                                "steps": list(episode.oracle.current_state.steps),
                                "constraint": episode.oracle.current_state.constraint})
    snapshot = world.snapshot()
    ctx = context(snapshot)
    assert utility_action_state_valid(ctx).outcome == "pass"
    assert utility_no_prohibited_effects(ctx).outcome == "pass"
    assert world.grade().success is True

    empty = context({})
    assert utility_action_state_valid(empty).outcome == "unsupported"
