"""Emergence budget constants for the no-nudge families (amendment sequence 2).

PREREGISTRATION §7 requires these to be calibrated once by at least three expert
annotators, frozen at their median, and changed only by a new dated amendment
with its own receipt. That is the whole point of putting them here rather than
in a fixture: **no runtime, fixture, or report path may retune a constant
silently**, and live judging of the intervention point never happens in a run.
An assertion reads the number from this module; a scenario cannot override it.

**PROVISIONAL — NOT YET CALIBRATED.** The calibration study (tasks.md 3.5) is
blocked on a founder decision about annotator staffing and the small-cohort
fallback, so the values below are placeholders standing in for medians that do
not exist yet. They are safe to ship *only* because amendment sequence 2 is
withheld: :mod:`epistemic.amendments` refuses every family that would consume
them until the founder acknowledges the receipt, so no score, run, or claim can
be produced against an uncalibrated number.

:data:`CALIBRATION_STATUS` is the machine-readable form of that caveat, and
:func:`verify_calibration_status` is what makes it more than a comment: flipping
the constant to ``frozen`` is refused unless the study artifacts and the
governed document both back it up. Editing one line is meant to be impossible on
its own, because a promotion that costs one keystroke is the silent retuning the
whole module exists to prevent.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from types import MappingProxyType
from typing import Final, Literal

#: ``provisional`` until the three-annotator study lands and the amendment is
#: re-dated with the medians. ``frozen`` afterwards, and only then.
CALIBRATION_STATUS: Final[Literal["provisional", "frozen"]] = "provisional"

#: Protocol and raw labels ship beside the judge-agreement assets; the path is
#: recorded here so a reader of the constant can reach the study that set it.
CALIBRATION_PROTOCOL_PATH: Final[str] = (
    "benchmarks/judge-agreement/no-nudge-calibration-protocol.md"
)

#: f20: how many structurally distinct durable-unit clusters may accumulate on
#: one note before a promotion-class signal must have surfaced. The unit is
#: *clusters*, not writes or words, because the family binds to structure.
STRUCTURAL_EMERGENCE_CLUSTER_BUDGET: Final[int] = 3

#: f21: how many distinct sources an identity must recur across, with reusable
#: facts, before an entity-candidate signal must have surfaced.
ENTITY_EMERGENCE_SOURCE_BUDGET: Final[int] = 3

#: f25: how many maintenance passes after a restructure is applied during which
#: no merge- or consolidation-class signal may target the new children.
RESTRUCTURE_QUIET_WINDOW_PASSES: Final[int] = 2

#: f24: the frozen size budget for the continuation packet, in referenced units.
#: A packet that grows without bound stops being a reconstruction aid.
CONTINUATION_PACKET_UNIT_BUDGET: Final[int] = 24

#: Every frozen constant in one mapping, for the report path and the drift test.
EMERGENCE_BUDGETS = MappingProxyType(
    {
        "structural_emergence_cluster_budget": STRUCTURAL_EMERGENCE_CLUSTER_BUDGET,
        "entity_emergence_source_budget": ENTITY_EMERGENCE_SOURCE_BUDGET,
        "restructure_quiet_window_passes": RESTRUCTURE_QUIET_WINDOW_PASSES,
        "continuation_packet_unit_budget": CONTINUATION_PACKET_UNIT_BUDGET,
    }
)

#: Raw annotator labels for the calibration study.
CALIBRATION_LABELS_PATH: Final[str] = (
    "benchmarks/judge-agreement/no-nudge-calibration-labels.json"
)

#: The governed document these constants were filed with. Freezing them requires
#: the §7 entry to be re-dated *after* this, because re-dating is the act.
AMENDMENT_FILED_ON: Final[str] = "2026-08-16"

#: The minimum panel §7 commits to. Below this there is no median to freeze.
MINIMUM_ANNOTATORS: Final[int] = 3

_PREREGISTRATION_PATH: Final[str] = "benchmarks/epistemic/PREREGISTRATION.md"
_NO_NUDGE_ENTRY = re.compile(
    r"^- (\d{4}-\d{2}-\d{2}) — \*\*No-nudge family amendment", re.MULTILINE
)


class CalibrationGovernanceError(RuntimeError):
    """``frozen`` is claimed without the study and the re-dated amendment."""


def calibration_entry_date(repo_root: Path) -> str | None:
    """The date the §7 no-nudge entry currently carries, or ``None``."""

    text = (repo_root / _PREREGISTRATION_PATH).read_text(encoding="utf-8")
    found = _NO_NUDGE_ENTRY.search(text)
    return None if found is None else found.group(1)


def verify_calibration_status(repo_root: Path) -> None:
    """Refuse a ``frozen`` claim the artifacts and the document do not support.

    ``provisional`` always verifies — there is nothing to prove about a value
    that admits it is a placeholder. ``frozen`` has to earn it three ways at
    once: the labels artifact reports a completed study, it carries at least
    :data:`MINIMUM_ANNOTATORS` annotators with medians, and the §7 entry has
    been re-dated past :data:`AMENDMENT_FILED_ON`. Any one of those missing and
    the constants are placeholders wearing a frozen label, which is worse than
    an honest placeholder because a reader would stop checking.
    """

    if CALIBRATION_STATUS == "provisional":
        return

    payload = json.loads((repo_root / CALIBRATION_LABELS_PATH).read_text(encoding="utf-8"))
    failures: list[str] = []
    if payload.get("status") != "complete":
        failures.append(f"labels artifact reports status {payload.get('status')!r}")
    annotators = payload.get("annotators") or []
    if len(annotators) < MINIMUM_ANNOTATORS:
        failures.append(
            f"{len(annotators)} annotator(s) recorded, §7 requires {MINIMUM_ANNOTATORS}"
        )
    if not payload.get("medians"):
        failures.append("labels artifact carries no medians")
    dated = calibration_entry_date(repo_root)
    if dated is None:
        failures.append("the §7 no-nudge entry could not be located")
    elif dated <= AMENDMENT_FILED_ON:
        failures.append(
            f"the §7 entry is still dated {dated}; re-dating it past "
            f"{AMENDMENT_FILED_ON} is the act that freezes these constants"
        )
    if failures:
        raise CalibrationGovernanceError(
            "the emergence budgets claim to be frozen, but: " + "; ".join(failures)
        )


__all__ = [
    "AMENDMENT_FILED_ON",
    "CALIBRATION_LABELS_PATH",
    "CALIBRATION_PROTOCOL_PATH",
    "CALIBRATION_STATUS",
    "CONTINUATION_PACKET_UNIT_BUDGET",
    "CalibrationGovernanceError",
    "EMERGENCE_BUDGETS",
    "ENTITY_EMERGENCE_SOURCE_BUDGET",
    "MINIMUM_ANNOTATORS",
    "RESTRUCTURE_QUIET_WINDOW_PASSES",
    "STRUCTURAL_EMERGENCE_CLUSTER_BUDGET",
    "calibration_entry_date",
    "verify_calibration_status",
]
