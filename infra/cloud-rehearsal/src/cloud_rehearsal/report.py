"""The machine-readable rehearsal report (tasks 5.2 and 5.3).

One JSON document: the environment and pinned inputs, every host
adaptation and chart overlay, each P3 step with its status, duration,
evidence and failure, the 5.3 measurements against their targets, and the
cross-lane defects the run exposed. The report never carries a secret: the
evidence it records is identifiers, codes, states and timings.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import time
import traceback
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

SCHEMA = "exomem-cloud-rehearsal-report-v1"

# design.md P3 and tasks 5.2/5.3.
TARGETS = {
    "initialize_warm_p95_seconds": 0.5,
    "tools_list_warm_p95_seconds": 0.5,
    "capture_p95_seconds": 1.0,
    "cited_recall_p95_seconds": 1.0,
    "provision_seconds": 180.0,
    "upgrade_seconds_per_cell": 60.0,
}

PASSED, FAILED, BLOCKED = "passed", "failed", "blocked"


class StepFailure(AssertionError):
    """A step's own check failed; the message says which and why."""


class CrossLaneDefect(StepFailure):
    """The failure is in cellctl, the chart or Substrate, not the rehearsal."""

    def __init__(self, message: str, *, component: str, owner: str, evidence: dict[str, Any]) -> None:
        super().__init__(message)
        self.component = component
        self.owner = owner
        self.evidence = evidence


@dataclass
class StepRecord:
    number: int
    name: str
    status: str = BLOCKED
    started_at: str | None = None
    seconds: float | None = None
    evidence: dict[str, Any] = field(default_factory=dict)
    failure: dict[str, Any] | None = None


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    rank = max(0, math.ceil(fraction * len(ordered)) - 1)
    return ordered[rank]


@dataclass
class Report:
    run_id: str
    started_at: str = field(default_factory=lambda: dt.datetime.now(dt.UTC).isoformat())
    finished_at: str | None = None
    valid: bool = True
    invalid_reasons: list[str] = field(default_factory=list)
    environment: dict[str, Any] = field(default_factory=dict)
    inputs: dict[str, Any] = field(default_factory=dict)
    adaptations: list[str] = field(default_factory=list)
    overlays: list[str] = field(default_factory=list)
    stages: dict[str, dict[str, Any]] = field(default_factory=dict)
    steps: list[StepRecord] = field(default_factory=list)
    samples: dict[str, list[float]] = field(default_factory=dict)
    single: dict[str, float] = field(default_factory=dict)
    defects: list[dict[str, Any]] = field(default_factory=list)

    def sample(self, name: str, seconds: float) -> None:
        self.samples.setdefault(name, []).append(round(seconds, 4))

    def measurements(self) -> dict[str, Any]:
        observed: dict[str, Any] = {
            "initialize_warm_p95_seconds": percentile(self.samples.get("initialize_warm", []), 0.95),
            "tools_list_warm_p95_seconds": percentile(self.samples.get("tools_list_warm", []), 0.95),
            "capture_p95_seconds": percentile(self.samples.get("capture", []), 0.95),
            "cited_recall_p95_seconds": percentile(self.samples.get("cited_recall", []), 0.95),
            "provision_seconds": self.single.get("provision_seconds"),
            "upgrade_seconds_per_cell": self.single.get("upgrade_seconds_per_cell"),
        }
        return {
            name: {
                "observed": None if value is None else round(value, 4),
                "target": TARGETS[name],
                "comparison": "<" if name == "provision_seconds" else "<=",
                "met": None if value is None else (value < TARGETS[name] if name == "provision_seconds" else value <= TARGETS[name]),
                "samples": len(self.samples.get(_sample_key(name), [])) or (1 if value is not None else 0),
            }
            for name, value in observed.items()
        }

    def to_json(self) -> dict[str, Any]:
        steps = [asdict(step) for step in self.steps]
        passed = all(step.status == PASSED for step in self.steps) and len(self.steps) == 12
        measurements = self.measurements()
        return {
            "schema": SCHEMA,
            "run_id": self.run_id,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "valid_rehearsal": self.valid,
            "invalid_reasons": self.invalid_reasons,
            "outcome": {
                "all_steps_passed": passed,
                "all_targets_met": all(m["met"] for m in measurements.values()),
                "steps": {status: sum(1 for s in self.steps if s.status == status) for status in (PASSED, FAILED, BLOCKED)},
                "gates_node": passed and self.valid and all(m["met"] for m in measurements.values()),
            },
            "environment": self.environment,
            "inputs": self.inputs,
            "adaptations": self.adaptations,
            "overlays": self.overlays,
            "stages": self.stages,
            "steps": steps,
            "measurements": measurements,
            "samples": self.samples,
            "cross_lane_defects": self.defects,
        }

    def write(self, path: Path) -> None:
        self.finished_at = dt.datetime.now(dt.UTC).isoformat()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_json(), indent=2, sort_keys=False, default=str) + "\n", encoding="utf-8")


def _sample_key(measurement: str) -> str:
    return {
        "initialize_warm_p95_seconds": "initialize_warm",
        "tools_list_warm_p95_seconds": "tools_list_warm",
        "capture_p95_seconds": "capture",
        "cited_recall_p95_seconds": "cited_recall",
    }.get(measurement, "")


def innermost(error: BaseException) -> BaseException:
    """The MCP client raises through anyio task groups; report the real cause."""

    while isinstance(error, BaseExceptionGroup) and len(error.exceptions) == 1:
        error = error.exceptions[0]
    return error


def failure_record(error: BaseException) -> dict[str, Any]:
    error = innermost(error)
    record: dict[str, Any] = {"type": type(error).__name__, "message": str(error)[:4000]}
    if isinstance(error, CrossLaneDefect):
        record.update({"cross_lane": True, "component": error.component, "owner": error.owner, "evidence": error.evidence})
    elif not isinstance(error, StepFailure):
        # A rehearsal bug or an environment failure, not a product finding:
        # keep the frame so it can be fixed.
        record["traceback"] = traceback.format_exception(error)[-6:]
    return record


class Timer:
    def __enter__(self) -> Timer:
        self.started = time.perf_counter()
        return self

    def __exit__(self, *_: object) -> None:
        self.seconds = time.perf_counter() - self.started
