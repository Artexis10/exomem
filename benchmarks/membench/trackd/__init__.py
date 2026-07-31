"""Track D: multi-step product workflow journeys for the membench benchmark.

Each journey drives ``python -m exomem <product_command> ... --json``
subprocesses against a fresh isolated vault (EXOMEM_VAULT_PATH always a
benchmark temp dir; lexical/deterministic profile env) and applies
deterministic checks — no model calls, no network, no manual interventions.

- ``journeys``: the journey framework (CommandRun / JourneyCheck /
  JourneyResult in the scripts/product_flow_benchmark.py style) plus J1
  (longitudinal evolution) and J2 (correction propagation).
- ``runner``: registry + report assembly (JSON-able).
"""

from membench.trackd.journeys import (
    CommandRun,
    JourneyCheck,
    JourneyResult,
    JourneyRunner,
    journey_env,
    run_j1_longitudinal,
    run_j2_correction,
)
from membench.trackd.runner import JOURNEYS, run_journeys

__all__ = [
    "CommandRun",
    "JOURNEYS",
    "JourneyCheck",
    "JourneyResult",
    "JourneyRunner",
    "journey_env",
    "run_j1_longitudinal",
    "run_j2_correction",
    "run_journeys",
]
