"""Seeded episode generation: determinism, isolation, and no private leaks."""

from __future__ import annotations

import dataclasses

import pytest

from membench.utility.schema import PHASES, VARIANTS
from membench.utility.scenarios import ScenarioError, actor_view, generate_episode


def test_same_seed_is_stable() -> None:
    first = generate_episode(7, "helpful_history")
    second = generate_episode(7, "helpful_history")
    assert first == second


def test_different_seed_differs() -> None:
    first = generate_episode(7, "helpful_history")
    second = generate_episode(8, "helpful_history")
    assert first.episode_id != second.episode_id
    assert first.oracle != second.oracle


def test_different_variant_same_seed_differs() -> None:
    a = generate_episode(7, "helpful_history")
    b = generate_episode(7, "self_contained")
    assert a.episode_id != b.episode_id
    assert a.oracle.target_project != "" and b.oracle.target_project != ""


def test_unknown_variant_rejected() -> None:
    with pytest.raises(ScenarioError):
        generate_episode(1, "bogus_variant")


@pytest.mark.parametrize("variant", VARIANTS)
def test_episode_has_three_ordered_phases(variant: str) -> None:
    episode = generate_episode(3, variant)
    assert [p.phase for p in episode.phases] == list(PHASES)


@pytest.mark.parametrize("variant", VARIANTS)
def test_oracle_procedure_has_two_or_three_steps_and_one_constraint(variant: str) -> None:
    episode = generate_episode(3, variant)
    assert 2 <= len(episode.oracle.current_state.steps) <= 3
    assert episode.oracle.current_state.constraint


def test_stale_distractor_changes_current_state_from_initial() -> None:
    episode = generate_episode(3, "stale_distractor")
    assert episode.oracle.changed is True
    assert episode.oracle.initial_state != episode.oracle.current_state
    assert episode.oracle.initial_state.project == episode.oracle.current_state.project


def test_non_stale_variants_do_not_change() -> None:
    for variant in ("helpful_history", "self_contained"):
        episode = generate_episode(3, variant)
        assert episode.oracle.changed is False
        assert episode.oracle.initial_state == episode.oracle.current_state


def _actor_visible_fields(view: object) -> set[str]:
    return {f.name for f in dataclasses.fields(view)}  # type: ignore[arg-type]


@pytest.mark.parametrize("variant", VARIANTS)
def test_actor_view_carries_no_private_metadata(variant: str) -> None:
    episode = generate_episode(11, variant)
    view = actor_view(episode, 0)
    # The actor-visible record has no seed/variant/oracle fields at all.
    assert _actor_visible_fields(view) == {"phase", "narrative"}
    for text in (view.phase, view.narrative):
        assert str(episode.seed) not in text
        assert variant not in text
    # The private oracle's target project may legitimately be *named* in the
    # narrative (it is what the actor is acting on) but the oracle object
    # itself, and its hash/seed, are never reachable from the view.
    assert not hasattr(view, "oracle")
    assert not hasattr(view, "seed")
    assert not hasattr(view, "variant")


def test_earlier_phase_views_contain_no_future_content() -> None:
    episode = generate_episode(5, "stale_distractor")
    experience = actor_view(episode, 0)
    change = actor_view(episode, 1)
    action = actor_view(episode, 2)
    # The change-phase notice text must not already appear during experience.
    assert change.narrative not in experience.narrative
    assert action.narrative not in experience.narrative
    assert action.narrative not in change.narrative


def test_actor_view_phase_index_out_of_range_rejected() -> None:
    episode = generate_episode(5, "helpful_history")
    with pytest.raises(ScenarioError):
        actor_view(episode, 3)
    with pytest.raises(ScenarioError):
        actor_view(episode, -1)


def test_current_target_oracle_state_is_internally_consistent() -> None:
    """The oracle's own current_state is exactly what a correct actor must
    submit; it must reference the target project and be well-formed."""

    for variant in VARIANTS:
        episode = generate_episode(9, variant)
        oracle = episode.oracle
        assert oracle.current_state.project == oracle.target_project
        assert oracle.other_state.project == oracle.other_project
        assert oracle.target_project != oracle.other_project
        assert len(set(oracle.current_state.steps)) == len(oracle.current_state.steps)
