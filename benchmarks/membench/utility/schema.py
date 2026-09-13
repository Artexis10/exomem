"""Typed records for the epistemic-utility action instrument.

Pure dataclasses, deliberately separate from ``membench.schema``'s pydantic
QA-corpus records: this slice grades resulting environment state, not
retrieval answers, and has no serialisation contract to keep byte-stable.

Two data are kept structurally apart everywhere in this module:

- **Actor-visible** data (:class:`PhaseView`, tool results) -- an opaque
  episode id and phase narrative text only.
- **Evaluator-only** data (:class:`Episode.oracle`, seed, variant name) --
  never handed to ``actor_view`` or the action world's tool surface.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

#: The three variants this slice implements. Order is fixed and deterministic
#: (used to derive each variant's independent child seed).
VARIANTS: tuple[str, ...] = ("helpful_history", "self_contained", "stale_distractor")

#: Ordered session phases every episode runs.
PHASES: tuple[str, ...] = ("experience", "change", "action")


@dataclass(frozen=True)
class ProjectState:
    """One project's ordered steps and single constraint at some instant."""

    project: str
    steps: tuple[str, ...]
    constraint: str


@dataclass(frozen=True)
class EpisodeOracle:
    """Evaluator-only grading target. Never exposed to an actor."""

    target_project: str
    other_project: str
    #: Authoritative state at episode start (before the "change" phase).
    initial_state: ProjectState
    #: Authoritative state a correct final action must match; equals
    #: ``initial_state`` unless the variant introduces a change.
    current_state: ProjectState
    #: Fixed, never-changing state of the decoy project, so a wrong-project
    #: write has somewhere real to land.
    other_state: ProjectState
    changed: bool


@dataclass(frozen=True)
class PhaseView:
    """Actor-visible narrative for one phase. No hidden fields."""

    phase: str
    narrative: str


@dataclass(frozen=True)
class Episode:
    """A seeded episode. ``oracle`` and ``seed`` are evaluator-only."""

    episode_id: str
    seed: int
    variant: str
    phases: tuple[PhaseView, ...]
    oracle: EpisodeOracle


class AttemptStatus(str, Enum):
    VALID = "valid"
    INVALID = "invalid"
    MISSING = "missing"


@dataclass(frozen=True)
class ActionOutcome:
    """What :meth:`ActionWorld.grade` reports for one attempt."""

    episode_id: str
    variant: str
    arm: str
    status: AttemptStatus
    success: bool | None
    destructive_effects: tuple[str, ...] = ()
    failure_reason: str | None = None


@dataclass(frozen=True)
class UsageRecord:
    """One phase's cost/time input. ``None`` means unknown, never zero."""

    episode_id: str
    variant: str
    arm: str
    phase: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    reasoning_tokens: int | None = None
    wall_time_s: float | None = None
    model_time_s: float | None = None
    tool_time_s: float | None = None
    turns: int | None = None
    peak_context_tokens: int | None = None
    cumulative_context_tokens: int | None = None
    cost_usd: float | None = None


@dataclass(frozen=True)
class UsageTotal:
    """A summed field: known values only, plus how many were unknown."""

    total: float | None
    known_count: int
    unknown_count: int


@dataclass(frozen=True)
class ArmOutcome:
    """Per (variant, arm) attempt accounting."""

    variant: str
    arm: str
    attempted: int
    valid: int
    invalid: int
    missing: int
    destructive_effect_counts: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class PairOutcome:
    """Per-variant paired outcome accounting."""

    variant: str
    scheduled: int
    attempted: int
    valid: int
    invalid: int
    missing: int
    wins: int
    losses: int
    both_pass: int
    both_fail: int
    utility_lift: float | None
    harm_numerator: int
    harm_denominator: int
    conditional_harm: float | None
    unconditional_losses: float | None
