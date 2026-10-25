"""The cost of a round, before it runs: sessions, and an estimate of tokens and time.

The estimate rests on a calibration: the means of a finished run of ``grouped``
sessions (the smoke run, until a dev round replaces it). An arm whose schema
size is unknown is estimated at ``grouped``'s size and says so; the arms differ
in schema bytes, which every request of a session re-sends, so a larger or
smaller schema moves input tokens by about its tokens times the requests.
"""

from __future__ import annotations

import json
import statistics
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path

from .readout import BASELINE
from .score import CaseResult

#: A rough ratio for JSON schema text, used only to carry a schema-size
#: difference between arms into tokens. An estimate, never a measurement.
BYTES_PER_TOKEN = 3.5


@dataclass(frozen=True)
class Calibration:
    """Means over the scored ``grouped`` sessions of one run."""

    source: str
    sessions: int
    input_tokens: float
    output_tokens: float
    calls: float
    wall_seconds: float
    schema_bytes: float

    def as_dict(self) -> dict:
        return asdict(self)


def calibrate(results: Sequence[CaseResult], *, source: str) -> Calibration:
    scored = [r for r in results if r.scored and r.arm == BASELINE and r.input_tokens is not None]
    if not scored:
        raise ValueError(f"no scored {BASELINE} sessions to calibrate from")

    def mean(name: str) -> float:
        return statistics.fmean(getattr(r, name) for r in scored if getattr(r, name) is not None)

    return Calibration(
        source=source,
        sessions=len(scored),
        input_tokens=mean("input_tokens"),
        output_tokens=mean("output_tokens"),
        calls=mean("calls"),
        wall_seconds=mean("wall_seconds"),
        schema_bytes=mean("schema_bytes"),
    )


def load_calibration(path: Path) -> Calibration:
    return Calibration(**json.loads(Path(path).read_text(encoding="utf-8")))


def estimate_round(
    *,
    cases: int,
    arms: Sequence[str],
    repeats: int,
    calibration: Calibration | None,
    schema_bytes: Mapping[str, int],
) -> dict:
    per_arm = cases * repeats
    estimate: dict = {"cases": cases, "arms": list(arms), "repeats": repeats, "sessions": per_arm * len(arms)}
    if calibration is None:
        return {**estimate, "input_tokens": None, "output_tokens": None, "wall_seconds": None, "notes": []}
    input_tokens = 0.0
    notes = []
    for arm in arms:
        known = arm == BASELINE or arm in schema_bytes
        size = schema_bytes.get(arm, calibration.schema_bytes) if arm != BASELINE else calibration.schema_bytes
        requests = calibration.calls + 1
        per_session = calibration.input_tokens + (size - calibration.schema_bytes) / BYTES_PER_TOKEN * requests
        input_tokens += per_session * per_arm
        if not known:
            notes.append(f"{arm}: schema size unknown, estimated at the {BASELINE} size")
    sessions = estimate["sessions"]
    return {
        **estimate,
        "input_tokens": round(input_tokens),
        "output_tokens": round(calibration.output_tokens * sessions),
        "wall_seconds": round(calibration.wall_seconds * sessions),
        "notes": notes,
    }


def format_plan(rounds: Mapping[str, Mapping], calibration: Calibration | None) -> list[str]:
    if calibration is None:
        lines = ["no calibration: token and time estimates are unavailable (pass --calibration or run the calibrate stage)"]
    else:
        lines = [
            f"calibration: {calibration.source}; {calibration.sessions} {BASELINE} sessions, "
            f"{calibration.input_tokens:,.0f} input and {calibration.output_tokens:,.0f} output tokens, "
            f"{calibration.calls:.1f} calls, {calibration.wall_seconds:.0f} s of turn time each (means)"
        ]
    for name, row in rounds.items():
        lines.append(
            f"round {name}: {row['cases']} cases x {len(row['arms'])} arms x {row['repeats']} repeats "
            f"= {row['sessions']} sessions"
        )
        if row["input_tokens"] is not None:
            lines.append(
                f"  estimate: {row['input_tokens']:,} input tokens (cache reads included), "
                f"{row['output_tokens']:,} output tokens, {row['wall_seconds'] / 3600:.1f} h of turn time, sessions one at a time (startup not included)"
            )
        lines.extend(f"  note: {note}" for note in row["notes"])
    return lines
