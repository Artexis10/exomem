"""The stage-C table: what each full session did, one line per (case, model, repeat) with the two arms side by side.

Every cell reads `baseline / candidate` from the rows that `session-close` wrote and, for wall time, from the `result`
events of the stream file the row names. A run that ended in an error, a missing stream and a missing reply are shown
as `unknown`, never as zero. No interval: the table is a smoke, not a measurement.
"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Sequence
from pathlib import Path

from . import checks, session_score
from .report import read_rows
from .session_close import stream_events

COLUMNS = ("tool calls", "recall_context calls", "vault writes", "reply characters", "wall seconds")
RECALL_TOOL = "recall_context"


def wall_seconds(row: dict) -> float | None:
    """The sum of the `duration_ms` of the stream's `result` events (one per turn); None when the stream is unreadable."""
    try:
        text = Path(row["stream"]).read_text(encoding="utf-8")
    except (KeyError, TypeError, OSError, UnicodeError):
        return None
    durations = [e.get("duration_ms") for e in stream_events(text) if e.get("type") == "result"]
    return sum(durations) / 1000 if durations and all(isinstance(d, int | float) for d in durations) else None


def measures(row: dict) -> tuple[float | None, ...]:
    """The five columns of one session; each is None when the run ended in an error or lacks its source."""
    if row["error"] is not None:
        return (None,) * len(COLUMNS)
    calls = row.get("calls")
    seen = row.get("observed")
    replies = row.get("replies")
    return (
        len(calls) if calls is not None else None,
        sum(str(call["name"]).endswith(RECALL_TOOL) for call in calls) if calls is not None else None,
        len(seen["changed"]) + len(seen["created"]) if seen else None,
        len(replies[-1]) if replies else None,
        wall_seconds(row),
    )


def cell(pair: tuple[float | None, float | None]) -> str:
    return " / ".join("unknown" if value is None else f"{value:g}" for value in pair)


def build(runs: Sequence[Path], *, baseline: str, candidate: str, judged: Path | None = None) -> str:
    rows = [r for r in read_rows(runs, identity=session_score.run_identity)[0] if r["arm"] in (baseline, candidate)]
    verdicts: dict[tuple, str] = {}
    if judged is not None:
        data = json.loads(judged.read_text(encoding="utf-8"))
        verdicts = {(r["scenario"], r["arm"], r["model"], r["repeat"], r["client_mode"]): r["verdict"] for r in data["rows"] if r["stage"] == "C"}
    pairs: dict[tuple, dict[str, dict]] = defaultdict(dict)
    for row in rows:
        pairs[(row["scenario"], row["model"], row["repeat"], row.get("client_mode"))][row["arm"]] = row
    out = [
        f"Stage-C sessions: {len(rows)} runs of baseline `{baseline}` and candidate `{candidate}`, {len(pairs)} (case, model, repeat) pairs.",
        "Each cell reads baseline / candidate. `vault writes` counts the seeded pages the session changed plus the pages it "
        "created; `wall seconds` sums the stream's result events. A run that ended in an error shows `unknown`.",
        "",
        "| case | model | repeat | " + " | ".join(COLUMNS) + " | reply judged (M1 and M2) |",
        "|---|---|---|" + "---|" * (len(COLUMNS) + 1),
    ]
    for (case, model, repeat, mode), by_arm in sorted(pairs.items(), key=lambda item: item[0][:3]):
        measured = [measures(by_arm[arm]) if arm in by_arm else (None,) * len(COLUMNS) for arm in (baseline, candidate)]
        judged_cells = [verdicts.get((case, arm, model, repeat, mode), checks.UNKNOWN) for arm in (baseline, candidate)]
        out.append(
            f"| {case} | {model} | {repeat} | "
            + " | ".join(cell((measured[0][i], measured[1][i])) for i in range(len(COLUMNS)))
            + f" | {' / '.join(judged_cells)} |"
        )
    return "\n".join(out) + "\n"
