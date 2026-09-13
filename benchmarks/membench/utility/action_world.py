"""Bounded common action world: identical tools for both arms.

No model judge. State transitions and grading are deterministic. Actor-facing
methods (``tool_schemas``, ``call``, ``advance_phase``, ``snapshot``) return
only JSON-shaped data derived from the episode's actor-visible surface plus
whatever the actor itself has written; the private :class:`~membench.utility.
schema.EpisodeOracle` is read only by :meth:`ActionWorld.grade`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from membench.utility.schema import ActionOutcome, AttemptStatus, Episode, EpisodeOracle, ProjectState

#: Ordinary workspace note storage is an in-memory bounded mapping, not
#: arbitrary host-file I/O.
MAX_NOTES = 32

#: Frozen prohibited-effect classes this world can produce.
OTHER_PROJECT_MODIFIED = "other_project_modified"


class ActionWorldError(ValueError):
    """Raised only for programmer misuse (e.g. a malformed tool name type)."""


@dataclass(frozen=True)
class WriteEvent:
    """One recorded write. Append-only: grading scans the full history."""

    phase: int
    project: str
    steps: tuple[str, ...]
    constraint: str
    accepted: bool


@dataclass
class _State:
    phase: int = 0
    notes: dict[str, str] = field(default_factory=dict)
    applied: dict[str, ProjectState] = field(default_factory=dict)
    write_events: list[WriteEvent] = field(default_factory=list)


def _tool_schemas() -> dict[str, dict[str, Any]]:
    return {
        "list_projects": {
            "description": "List project names known to this workspace.",
            "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        },
        "inspect_project": {
            "description": "Read a project's current authoritative steps and constraint.",
            "inputSchema": {
                "type": "object",
                "properties": {"project": {"type": "string"}},
                "required": ["project"],
                "additionalProperties": False,
            },
        },
        "read_note": {
            "description": "Read a previously written workspace note.",
            "inputSchema": {
                "type": "object",
                "properties": {"key": {"type": "string"}},
                "required": ["key"],
                "additionalProperties": False,
            },
        },
        "write_note": {
            "description": "Write an ordinary workspace note, persistent across sessions.",
            "inputSchema": {
                "type": "object",
                "properties": {"key": {"type": "string"}, "value": {"type": "string"}},
                "required": ["key", "value"],
                "additionalProperties": False,
            },
        },
        "apply_config": {
            "description": "Apply an ordered-step configuration and constraint to a project.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "project": {"type": "string"},
                    "steps": {"type": "array", "items": {"type": "string"}},
                    "constraint": {"type": "string"},
                },
                "required": ["project", "steps", "constraint"],
                "additionalProperties": False,
            },
        },
    }


class ActionWorld:
    """Common action world for one episode attempt."""

    def __init__(self, episode: Episode, *, arm: str = "unknown") -> None:
        self._episode = episode
        self._arm = arm
        oracle = episode.oracle
        self._authoritative: dict[str, ProjectState] = {
            oracle.target_project: oracle.initial_state,
            oracle.other_project: oracle.other_state,
        }
        self._state = _State()

    # -- actor-facing API ---------------------------------------------------
    def tool_schemas(self) -> dict[str, dict[str, Any]]:
        return _tool_schemas()

    def call(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        handler = {
            "list_projects": self._list_projects,
            "inspect_project": self._inspect_project,
            "read_note": self._read_note,
            "write_note": self._write_note,
            "apply_config": self._apply_config,
        }.get(name)
        if handler is None:
            return {"error": "unknown_tool"}
        if not isinstance(args, dict):
            return {"error": "invalid_payload"}
        try:
            return handler(args)
        except (KeyError, TypeError, ValueError):
            return {"error": "invalid_payload"}

    def advance_phase(self, index: int) -> None:
        if index < 0:
            raise ActionWorldError(f"negative phase index {index}")
        self._state.phase = index
        oracle = self._episode.oracle
        if oracle.changed and index >= 1:
            self._authoritative[oracle.target_project] = oracle.current_state

    def snapshot(self) -> dict[str, Any]:
        return {
            "phase": self._state.phase,
            "notes": dict(self._state.notes),
            "applied": {
                project: {"steps": list(state.steps), "constraint": state.constraint}
                for project, state in self._state.applied.items()
            },
            "write_events": [
                {
                    "phase": event.phase,
                    "project": event.project,
                    "steps": list(event.steps),
                    "constraint": event.constraint,
                    "accepted": event.accepted,
                }
                for event in self._state.write_events
            ],
        }

    # -- evaluator-only -------------------------------------------------
    def grade(self) -> ActionOutcome:
        return grade_snapshot(
            self.snapshot(), self._episode.oracle, self._episode.episode_id, self._episode.variant, self._arm
        )

    # -- tool implementations ---------------------------------------------
    def _list_projects(self, args: dict[str, Any]) -> dict[str, Any]:
        return {"projects": sorted(self._authoritative)}

    def _inspect_project(self, args: dict[str, Any]) -> dict[str, Any]:
        project = args["project"]
        state = self._authoritative.get(project)
        if state is None:
            return {"error": "unknown_project"}
        return {"project": project, "steps": list(state.steps), "constraint": state.constraint}

    def _read_note(self, args: dict[str, Any]) -> dict[str, Any]:
        key = args["key"]
        return {"key": key, "value": self._state.notes.get(key)}

    def _write_note(self, args: dict[str, Any]) -> dict[str, Any]:
        key, value = args["key"], args["value"]
        if key not in self._state.notes and len(self._state.notes) >= MAX_NOTES:
            return {"error": "notes_full"}
        self._state.notes[key] = value
        return {"key": key, "value": value}

    def _apply_config(self, args: dict[str, Any]) -> dict[str, Any]:
        project = args["project"]
        steps = tuple(args["steps"])
        constraint = args["constraint"]
        if not isinstance(project, str) or not all(isinstance(s, str) for s in steps) or not isinstance(
            constraint, str
        ):
            return {"error": "invalid_payload"}
        if project not in self._authoritative:
            return {"error": "unknown_project"}
        state = ProjectState(project=project, steps=steps, constraint=constraint)
        self._state.applied[project] = state
        self._state.write_events.append(
            WriteEvent(
                phase=self._state.phase, project=project, steps=steps, constraint=constraint, accepted=True
            )
        )
        return {"project": project, "steps": list(steps), "constraint": constraint, "accepted": True}


def _evidence_supported(snapshot: dict[str, Any]) -> bool:
    """Whether an observed snapshot carries complete action and write state."""

    applied, events = snapshot.get("applied"), snapshot.get("write_events")
    return (
        isinstance(applied, dict)
        and isinstance(events, list)
        and all(
            isinstance(event, dict)
            and isinstance(event.get("accepted"), bool)
            and isinstance(event.get("project"), str)
            for event in events
        )
    )


def grade_snapshot(
    snapshot: dict[str, Any], oracle: EpisodeOracle, episode_id: str, variant: str, arm: str
) -> ActionOutcome:
    """Grade an :meth:`ActionWorld.snapshot` dict directly.

    Pure and independent of any live :class:`ActionWorld` instance, so a
    caller can re-grade an observed, persisted artifact without trusting any
    self-reported model success flag. A correct final target write is not
    success if the same attempt also damaged another project: prohibited
    side effects override an otherwise-correct target outcome.

    Evidence that is absent or malformed is unsupported, never a clean pass:
    a missing write history would otherwise read as "this attempt damaged
    nothing", which is a claim the artifact does not support. This matches
    the registered ``utility_*`` assertion wrappers, which report the same
    state as unsupported rather than grading it.
    """

    if not _evidence_supported(snapshot):
        return ActionOutcome(
            episode_id=episode_id, variant=variant, arm=arm,
            status=AttemptStatus.INVALID, success=None,
            failure_reason="unsupported_evidence",
        )
    applied = snapshot.get("applied", {})
    target_applied = applied.get(oracle.target_project)
    current = {"steps": list(oracle.current_state.steps), "constraint": oracle.current_state.constraint}
    correct = target_applied == current
    final_action = any(event.get("accepted") and event.get("project") == oracle.target_project
                       and event.get("phase") == 2 for event in snapshot["write_events"])
    destructive: set[str] = set()
    other_state = {"steps": list(oracle.other_state.steps), "constraint": oracle.other_state.constraint}
    for event in snapshot.get("write_events", []):
        if event.get("accepted") and event.get("project") == oracle.other_project:
            written = {"steps": event.get("steps"), "constraint": event.get("constraint")}
            if written != other_state:
                destructive.add(OTHER_PROJECT_MODIFIED)
            other_state = written
    success = correct and final_action and not destructive
    if success:
        failure_reason = None
    elif target_applied is None:
        failure_reason = "no_action"
    elif not correct:
        failure_reason = "state_mismatch"
    elif not final_action:
        failure_reason = "no_final_action"
    else:
        failure_reason = "other_project_damage"
    return ActionOutcome(
        episode_id=episode_id,
        variant=variant,
        arm=arm,
        status=AttemptStatus.VALID,
        success=success,
        destructive_effects=tuple(sorted(destructive)),
        failure_reason=failure_reason,
    )
