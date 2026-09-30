"""Utility assertions observe world state, never actor self-reported success."""
from dataclasses import replace

from epistemic.assertions import AssertionContext
from epistemic.snapshot import EpistemicStateSnapshot
from membench.utility.action_world import ActionWorld
from membench.utility.scenarios import generate_episode


def context(world, episode):
    return AssertionContext(snapshot=EpistemicStateSnapshot(
        provider='fixture', variant='utility', phase='action',
        taken_at='2026-09-13T00:00:00Z', items=(), relations=(), declarations=(),
        projector={'name': 'utility', 'version': '1', 'author': 'fixture', 'endpoints_used': (), 'loc': 0}),
        utility_world_snapshot=world.snapshot(), utility_oracle=episode.oracle)


def test_utility_assertions_require_observed_evidence():
    from epistemic.assertions import utility_action_state_valid, utility_no_prohibited_effects
    episode = generate_episode(11, 'stale_distractor')
    ctx = context(ActionWorld(episode), episode)
    missing = replace(ctx, utility_world_snapshot=None)
    assert utility_action_state_valid(missing).outcome == 'unsupported'
    assert utility_no_prohibited_effects(missing).outcome == 'unsupported'


def test_utility_grades_state_and_preserves_damage_after_repair():
    from epistemic.assertions import utility_action_state_valid, utility_no_prohibited_effects
    episode = generate_episode(11, 'stale_distractor')
    world = ActionWorld(episode)
    world.advance_phase(2)
    assert utility_action_state_valid(context(world, episode)).outcome == 'fail'
    target = episode.oracle.current_state
    action = {'project': target.project, 'steps': list(target.steps), 'constraint': target.constraint}
    world.call('apply_config', action)
    assert utility_action_state_valid(context(world, episode)).outcome == 'pass'
    assert utility_no_prohibited_effects(context(world, episode)).outcome == 'pass'
    world.call('apply_config', {**action, 'project': episode.oracle.other_project})
    world.call('apply_config', action)
    assert utility_no_prohibited_effects(context(world, episode)).outcome == 'fail'


def test_missing_write_history_cannot_claim_no_damage():
    from epistemic.assertions import utility_action_state_valid, utility_no_prohibited_effects
    episode = generate_episode(11, 'helpful_history')
    world = ActionWorld(episode)
    target = episode.oracle.current_state
    world.call('apply_config', {'project': target.project, 'steps': list(target.steps), 'constraint': target.constraint})
    observed = world.snapshot()
    del observed['write_events']
    ctx = replace(context(world, episode), utility_world_snapshot=observed)
    assert utility_action_state_valid(ctx).outcome == 'unsupported'
    assert utility_no_prohibited_effects(ctx).outcome == 'unsupported'
