"""Seeded episode generation for the utility action instrument.

Reuses :class:`membench.templates.base.BuildContext` for deterministic naming
and :mod:`membench.procedural` for ordered-steps/precondition authoring and
its revision (supersession) machinery, rather than inventing a parallel
truth generator. The procedural claims are only a naming/consistency device
here: this instrument grades resulting action-world state, not retrieval, so
no query/expectation records are produced.
"""

from __future__ import annotations

from membench.ids import stable_id
from membench.templates.base import BuildContext  # import before membench.procedural: see below

# membench.procedural imports membench.templates.base, and membench.templates'
# package __init__ registers t17_procedural_chains, which imports membench.procedural
# back. Importing templates.base first (above) completes that module before
# procedural's import chain can observe it half-initialized.
from membench.procedural import author_procedure, current_order, distinct_nouns, revise_step
from membench.utility.schema import PHASES, VARIANTS, Episode, EpisodeOracle, PhaseView, ProjectState

_TEMPLATE_ID = "utility_action_episode"


class ScenarioError(ValueError):
    """Raised for an unknown variant or an internal generation inconsistency."""


def _variant_index(variant: str) -> int:
    if variant not in VARIANTS:
        raise ScenarioError(f"unknown variant {variant!r}; expected one of {VARIANTS}")
    return VARIANTS.index(variant)


def generate_episode(seed: int, variant: str) -> Episode:
    """Build one seeded episode. Deterministic in ``(seed, variant)``.

    The returned :class:`Episode` carries the private ``oracle`` and ``seed``
    fields for the evaluator; :func:`actor_view` is the only sanctioned way
    to derive what an actor may see.
    """

    index = _variant_index(variant)
    ctx = BuildContext(_TEMPLATE_ID, index, seed)
    nouns = distinct_nouns(ctx.rng, 8)
    target_name = ctx.project()
    other_name = ctx.project()

    step_labels = [f"{noun} step" for noun in nouns[:3]]
    constraint_label = f"{nouns[3]} clearance"
    proc = author_procedure(
        ctx,
        week=1,
        name=target_name,
        labels=step_labels,
        precondition_label=constraint_label,
    )

    changed = variant == "stale_distractor"
    revision = None
    if changed:
        replacement_label = f"{nouns[4]} step"
        revision = revise_step(
            ctx, proc, week=2, position=2, replacement_label=replacement_label
        )
    labels, _, _ = current_order(proc, revision)
    initial_state = ProjectState(
        project=target_name, steps=tuple(step_labels), constraint=constraint_label
    )
    current_state = ProjectState(
        project=target_name, steps=tuple(labels), constraint=constraint_label
    )

    other_labels = [f"{nouns[5]} step", f"{nouns[6]} step"]
    other_state = ProjectState(
        project=other_name, steps=tuple(other_labels), constraint=f"{nouns[7]} clearance"
    )

    oracle = EpisodeOracle(
        target_project=target_name,
        other_project=other_name,
        initial_state=initial_state,
        current_state=current_state,
        other_state=other_state,
        changed=changed,
    )

    episode_id = stable_id("EPI", str(seed), variant)
    phases = _phase_views(variant, oracle)
    return Episode(episode_id=episode_id, seed=seed, variant=variant, phases=phases, oracle=oracle)


def _steps_sentence(state: ProjectState) -> str:
    ordered = ", then ".join(state.steps)
    return (
        f"Project {state.project} configuration procedure: {ordered}. "
        f"Constraint: {state.constraint} must be satisfied first."
    )


def _phase_views(variant: str, oracle: EpisodeOracle) -> tuple[PhaseView, ...]:
    target = oracle.target_project
    if variant == "helpful_history":
        experience = (
            f"You configured {target}. {_steps_sentence(oracle.initial_state)} "
            "The configuration was applied and accepted."
        )
        change = (
            f"Unrelated note: project {oracle.other_project} also has a configuration "
            f"procedure ({_steps_sentence(oracle.other_state)}). It does not apply to "
            f"{target}."
        )
        action = f"Apply the current configuration for project {target}."
    elif variant == "self_contained":
        experience = "You opened a new workspace. No prior configuration work has happened yet."
        change = (
            f"Unrelated note: project {oracle.other_project} also has a configuration "
            f"procedure ({_steps_sentence(oracle.other_state)}). It does not apply to "
            f"{target}."
        )
        action = (
            f"Apply the configuration for project {target}. "
            f"{_steps_sentence(oracle.current_state)}"
        )
    elif variant == "stale_distractor":
        experience = (
            f"You configured {target}. {_steps_sentence(oracle.initial_state)} "
            "The configuration was applied and accepted."
        )
        change = (
            f"Project {target}'s configuration procedure changed. The prior steps are "
            "retired; check the current authoritative procedure before acting again."
        )
        action = f"Apply the current configuration for project {target}."
    else:  # pragma: no cover - guarded by _variant_index
        raise ScenarioError(f"unknown variant {variant!r}")
    narratives = {"experience": experience, "change": change, "action": action}
    return tuple(PhaseView(phase=phase, narrative=narratives[phase]) for phase in PHASES)


def actor_view(episode: Episode, phase_index: int) -> PhaseView:
    """The single phase view an actor may see. Never the oracle or seed."""

    if not 0 <= phase_index < len(episode.phases):
        raise ScenarioError(f"phase index {phase_index} out of range")
    return episode.phases[phase_index]
