"""One blind judge batch for the replay's stage-B answers and stage-C replies.

`prepare` writes every answer into one shuffled file of items under opaque ids and the private map that binds each id to
its run. An item holds the turn, the reference, the reply and the criteria of the scenario, and nothing that names the
arm, the model or the stage. A seeded fraction of the items appears twice under two ids. `import_judgments` reads the
judgments back by id. When fewer than `AGREEMENT_FLOOR` of the doubled items get the same pass or fail twice, the
result reads "incomplete: judge agreement X". Items and judgments have the shapes of `session-score`: the item is
`{"id", "prompt", "answer_schema"}` and a judgment is `{"id", "structured_output"}`. No model starts here.
"""

from __future__ import annotations

import json
import math
import random
import uuid
from collections.abc import Sequence
from pathlib import Path

from . import checks, judge, session_score
from .report import read_rows
from .scripted_replies import Scenario

REJUDGE_FRACTION = 0.2
AGREEMENT_FLOOR = 0.9
#: Fixed and written into the private map, so the same runs always double the same items.
BATCH_SEED = 20261110


def answers_of(stage_b: Sequence[Path], stage_c: Sequence[Path]) -> list[tuple[dict, str, str]]:
    """`(run, scenario id, reply)` for each run that has a reply. A run that ended in an error, or has no reply, has
    nothing to judge: the reports count it as unknown."""
    found = []
    for row in read_rows(stage_b)[0]:
        if row["error"] is None and row.get("final_text"):
            found.append(({"stage": "B", **{k: row[k] for k in ("scenario", "arm", "model", "repeat")}}, row["scenario"], row["final_text"]))
    for row in read_rows(stage_c, identity=session_score.run_identity)[0]:
        replies = row.get("replies") or []
        if row["error"] is None and replies and replies[-1]:
            found.append(({"stage": "C", "client_mode": row.get("client_mode"), **{k: row[k] for k in ("scenario", "arm", "model", "repeat")}}, row["scenario"], replies[-1]))
    return found


def prepare(stage_b: Sequence[Path], stage_c: Sequence[Path], scenarios: Sequence[Scenario], *, out: Path) -> int:
    """Write `judge-views.jsonl` and `private/judge-map.json` under `out`; returns the item count."""
    by_id = {s.id: s for s in scenarios}
    entries = []
    for run, scenario_id, reply in answers_of(stage_b, stage_c):
        if scenario_id not in by_id:
            raise SystemExit(f"a run names the case {scenario_id!r}, which has no scenario in the scenario folder")
        scenario = by_id[scenario_id]
        view = judge.render_answer_view(scenario.prompt, scenario.reference, reply)
        entries.append((run, scenario.criteria, judge.judge_prompt(view, scenario.criteria, answer_view=True)))
    rng = random.Random(BATCH_SEED)
    doubled = set(rng.sample(range(len(entries)), math.ceil(len(entries) * REJUDGE_FRACTION)))
    items, mapping = [], {}
    for position, (run, criteria, prompt) in enumerate(entries):
        first = uuid.uuid4().hex
        for item_id, twin_of in [(first, None), *([(uuid.uuid4().hex, first)] if position in doubled else [])]:
            items.append({"id": item_id, "prompt": prompt, "answer_schema": judge.answer_schema(criteria)})
            mapping[item_id] = {"run": run, "criteria": list(criteria), "twin_of": twin_of}
    rng.shuffle(items)
    (out / "private").mkdir(parents=True, exist_ok=True)
    (out / "judge-views.jsonl").write_text("".join(json.dumps(item) + "\n" for item in items), encoding="utf-8")
    (out / "private" / "judge-map.json").write_text(json.dumps({"seed": BATCH_SEED, "items": mapping}, indent=1) + "\n", encoding="utf-8")
    return len(items)


def item_verdict(entry: dict, judgments: dict) -> tuple[str, list[checks.Outcome]]:
    """The item's pass, fail or unknown, from its own judgment line; a missing, duplicate or malformed one is unknown."""
    criteria = tuple(entry["criteria"])
    outcomes = checks.judged_outcomes(criteria, session_score.valid_answer(judgments.get(entry["id"]), criteria))
    return checks.verdict(None, [], outcomes), outcomes


def import_judgments(judgments: Path, *, out: Path) -> dict:
    """Read the judgments of one batch; write `judged.json` under `out`. Starts no model."""
    mapping, lines = session_score.read_native(judgments, out / "private" / "judge-map.json")
    items = {item_id: {**entry, "id": item_id} for item_id, entry in (mapping.get("items") or {}).items()}
    if not items:
        raise SystemExit(f"{out / 'private' / 'judge-map.json'} holds no items; run judge-prepare first")
    rows, agreed, usable = [], 0, 0
    doubled = [entry for entry in items.values() if entry["twin_of"] is not None]
    for entry in items.values():
        verdict, outcomes = item_verdict(entry, lines)
        if entry["twin_of"] is None:
            rows.append({**entry["run"], "verdict": verdict, "criteria": [o.__dict__ for o in outcomes]})
            continue
        first_verdict, _ = item_verdict(items[entry["twin_of"]], lines)
        if {verdict, first_verdict} <= {checks.PASS, checks.FAIL}:
            usable += 1
            agreed += verdict == first_verdict
    rate = agreed / usable if usable else None
    incomplete = rate is None or rate < AGREEMENT_FLOOR
    summary = {"doubled_items": len(doubled), "usable_pairs": usable, "agreeing_pairs": agreed, "agreement": rate, "incomplete": incomplete}
    asked = list(dict.fromkeys(criterion["id"] for entry in items.values() for criterion in entry["criteria"]))
    judged = {"seed": mapping.get("seed"), "criteria": asked, "agreement": summary, "rows": rows}
    (out / "judged.json").write_text(json.dumps(judged, indent=1) + "\n", encoding="utf-8")
    return summary


def agreement_line(summary: dict) -> str:
    """The sentence a report leads with: the agreement of the doubled items, or `incomplete: judge agreement X`."""
    shown = "unknown (no doubled item has two usable judgments)" if summary["agreement"] is None else f"{summary['agreement']:.2f}"
    detail = f"{summary['agreeing_pairs']} of {summary['usable_pairs']} usable pairs of {summary['doubled_items']} doubled items agree on pass or fail"
    return f"{'incomplete: ' if summary['incomplete'] else ''}judge agreement {shown} ({detail})"
