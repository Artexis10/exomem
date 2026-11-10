"""One blind judge batch for the replay's stage-B answers and stage-C replies.

`prepare` writes every answer into one shuffled file of items under opaque ids and the private map that binds each id to
its run and to the sha256 of its prompt. An item holds the turn, the reference, the reply and the criteria of the
scenario, and nothing that names the arm, the model or the stage. A seeded fraction of the items appears twice under two
ids. `import_judgments` reads the judgments back by id, against the runs and scenarios as they are then: a run whose
prompt no longer has the prepared sha256 (the answer was rerun, or its reference or criteria changed) is `unknown`, as
`session_score.native_answers` does. When fewer than `AGREEMENT_FLOOR` of the doubled items get the same pass or fail
twice, the result reads "incomplete: judge agreement X". Items and judgments have the shapes of `session-score`: the item is
`{"id", "prompt", "answer_schema"}` and a judgment is `{"id", "structured_output"}`. Only `judge_all` starts a model: it
asks the judge about each item in a judge process of its own (`session_score.call_judge`).
"""

from __future__ import annotations

import hashlib
import json
import math
import random
import tempfile
import uuid
from collections.abc import Sequence
from pathlib import Path

from jsonschema import Draft202012Validator

from . import checks, judge, runner, session_score
from .report import read_rows
from .scripted_replies import Scenario

REJUDGE_FRACTION = 0.2
AGREEMENT_FLOOR = 0.9
#: Fixed and written into the private map, so the same runs always double the same items.
BATCH_SEED = 20261110
VIEWS_FILE = "judge-views.jsonl"
JUDGMENTS_FILE = "judgments.jsonl"
#: A missing or malformed judgment is asked for once more; after that the item stays without a judgment (unknown).
JUDGE_ATTEMPTS = 2


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


def blind_items(stage_b: Sequence[Path], stage_c: Sequence[Path], scenarios: Sequence[Scenario]) -> list[tuple[dict, tuple[dict, ...], str]]:
    """`(run, criteria, prompt)` for each run with a reply: the blind prompt that the judge reads for it."""
    by_id = {s.id: s for s in scenarios}
    entries = []
    for run, scenario_id, reply in answers_of(stage_b, stage_c):
        if scenario_id not in by_id:
            raise SystemExit(f"a run names the case {scenario_id!r}, which has no scenario in the scenario folder")
        scenario = by_id[scenario_id]
        view = judge.render_answer_view(scenario.prompt, scenario.reference, reply)
        entries.append((run, scenario.criteria, judge.judge_prompt(view, scenario.criteria, answer_view=True)))
    return entries


def prompt_sha256(prompt: str) -> str:
    return hashlib.sha256(prompt.encode()).hexdigest()


def run_id(run: dict) -> str:
    """A run's identity as one comparable value: the map stores runs as JSON objects."""
    return json.dumps(run, sort_keys=True)


def prepare(stage_b: Sequence[Path], stage_c: Sequence[Path], scenarios: Sequence[Scenario], *, out: Path) -> int:
    """Write `judge-views.jsonl` and `private/judge-map.json` under `out`; returns the item count."""
    entries = blind_items(stage_b, stage_c, scenarios)
    rng = random.Random(BATCH_SEED)
    doubled = set(rng.sample(range(len(entries)), math.ceil(len(entries) * REJUDGE_FRACTION)))
    items, mapping = [], {}
    for position, (run, criteria, prompt) in enumerate(entries):
        first = uuid.uuid4().hex
        for item_id, twin_of in [(first, None), *([(uuid.uuid4().hex, first)] if position in doubled else [])]:
            items.append({"id": item_id, "prompt": prompt, "answer_schema": judge.answer_schema(criteria)})
            mapping[item_id] = {"run": run, "criteria": list(criteria), "twin_of": twin_of, "prompt_sha256": prompt_sha256(prompt)}
    rng.shuffle(items)
    (out / "private").mkdir(parents=True, exist_ok=True)
    (out / VIEWS_FILE).write_text("".join(json.dumps(item) + "\n" for item in items), encoding="utf-8")
    (out / "private" / "judge-map.json").write_text(json.dumps({"seed": BATCH_SEED, "items": mapping}, indent=1) + "\n", encoding="utf-8")
    return len(items)


def read_views(out: Path) -> list[dict]:
    """The blind items that judge-prepare wrote under `out`."""
    path = out / VIEWS_FILE
    try:
        items = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    except (OSError, ValueError) as error:
        raise SystemExit(f"cannot read {path}: {error}; run judge-prepare first") from error
    if not items:
        raise SystemExit(f"{path} holds no items")
    return items


def judged_ids(path: Path) -> set[str]:
    """The ids that already have a line in a judgments file. The runner writes a line only for a valid answer, and a
    second line for an id would read as a duplicate (unknown), so an id with a line is never asked again."""
    found = set()
    for line in path.read_text(encoding="utf-8").splitlines() if path.exists() else []:
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict) and isinstance(item.get("id"), str):
            found.add(item["id"])
    return found


def ask(item: dict, *, model: str, executable: Path, scratch: Path) -> dict | None:
    """The judge's answer to one item, from processes of its own, or None after `JUDGE_ATTEMPTS` invalid answers."""
    for _attempt in range(JUDGE_ATTEMPTS):
        stdout = session_score.call_judge(item["prompt"], item["answer_schema"], model=model, executable=executable, scratch=scratch)
        answer = judge.structured_output(stdout or "")
        if answer is not None and Draft202012Validator(item["answer_schema"]).is_valid(answer):
            return answer
    return None


def judge_all(out: Path, *, model: str, executable: Path) -> tuple[int, int, list[str]]:
    """Judge every item of `judge-views.jsonl` under `out` that has no line yet in `judgments.jsonl`, each in a judge
    process of its own, so the two copies of a doubled item are judged apart. Reads the views only, never the private
    map. Appends one line per valid answer as it comes, so an interrupted run loses nothing and a rerun asks only the
    items still without a line. Returns the item count, the count judged now and the ids still without a judgment."""
    items = read_views(out)
    target = out / JUDGMENTS_FILE
    todo = [item for item in items if item["id"] not in judged_ids(target)]
    missing = []
    with tempfile.TemporaryDirectory(prefix="ig-judge-") as scratch, target.open("a", encoding="utf-8") as sink:
        if target.stat().st_size and not target.read_bytes().endswith(b"\n"):
            sink.write("\n")  # A line cut off by an interrupted run must not swallow the next one.
        for item in todo:
            answer = ask(item, model=model, executable=executable, scratch=Path(scratch))
            if answer is None:
                missing.append(item["id"])
                continue
            sink.write(json.dumps({"id": item["id"], "structured_output": answer}) + "\n")
            sink.flush()
    return len(items), len(todo) - len(missing), missing


def judge_plan(out: Path, *, model: str) -> list[str]:
    """What `judge_all` would run, as lines to print; starts nothing."""
    items = read_views(out)
    todo = [item for item in items if item["id"] not in judged_ids(out / JUDGMENTS_FILE)]
    lines = [
        f"Would judge {len(todo)} of {len(items)} items in {out / VIEWS_FILE} with {model}, one judge process per item "
        f"(at most {JUDGE_ATTEMPTS} when the answer does not match the schema), appending to {out / JUDGMENTS_FILE}."
    ]
    if todo:
        first = todo[0]
        argv = runner.judge_argv(
            Path("claude"), f"<the prompt of item {first['id']}: {len(first['prompt'])} characters>", first["answer_schema"],
            empty_mcp=Path("empty-mcp.json"), model=model,
        )
        lines.append("  each call, as for the first item: " + " ".join(argv[:2] + [repr(argv[2])] + argv[3:]))
    return lines


def item_verdict(entry: dict, judgments: dict) -> tuple[str, list[checks.Outcome]]:
    """The item's pass, fail or unknown, from its own judgment line; a missing, duplicate or malformed one is unknown."""
    criteria = tuple(entry["criteria"])
    outcomes = checks.judged_outcomes(criteria, session_score.valid_answer(judgments.get(entry["id"]), criteria))
    return checks.verdict(None, [], outcomes), outcomes


def import_judgments(
    judgments: Path, *, out: Path, stage_b: Sequence[Path], stage_c: Sequence[Path], scenarios: Sequence[Scenario]
) -> dict:
    """Read the judgments of one batch against the runs and scenarios that judge-prepare read, as they are now; write
    `judged.json` under `out`. Starts no model."""
    mapping, lines = session_score.read_native(judgments, out / "private" / "judge-map.json")
    items = {item_id: {**entry, "id": item_id} for item_id, entry in (mapping.get("items") or {}).items()}
    if not items:
        raise SystemExit(f"{out / 'private' / 'judge-map.json'} holds no items; run judge-prepare first")
    current = {run_id(run): prompt_sha256(prompt) for run, _criteria, prompt in blind_items(stage_b, stage_c, scenarios)}
    rows, agreed, usable = [], 0, 0
    doubled = [entry for entry in items.values() if entry["twin_of"] is not None]
    for entry in items.values():
        verdict, outcomes = item_verdict(entry, lines)
        if entry["twin_of"] is None:
            if current.get(run_id(entry["run"])) != entry.get("prompt_sha256"):
                # Bound to the exact prompt: a rerun answer or a changed reference must not inherit this judgment.
                stale = "the run's answer, reference or criteria changed after judge-prepare; prepare and judge it again"
                verdict, outcomes = checks.UNKNOWN, [checks.Outcome(c["id"], checks.UNKNOWN, stale) for c in entry["criteria"]]
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
