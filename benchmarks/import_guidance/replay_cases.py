"""Write one context-replay scenario per case, from a case folder that lives outside the repository.

The evaluator owns the cases, their seeds and their keys. A case file names a seed, one user turn and an engagement;
`<case-dir>/answer-keys/<id>.marks.json` holds the reference, the set (`S` or `N`) and the closed answer checks.
The scenario that comes out records the arm's real `read_instructions` and `recall_context` replies on the case seed
(`recording.py`), serves only those two replies to the actor, stages the arm's `UserPromptSubmit` hook and leaves the
judging to one blind batch (`judge_batch.py`). The judged criteria are the evaluator's too: `<case-dir>/rubric.json`
holds them, and the writer copies them into every scenario. Nothing is copied back into the repository: the writer
refuses an output folder under any repository.
"""

from __future__ import annotations

import json
from pathlib import Path

from epistemic.journeys.f27_replay import JourneySetupError, refuse_unsafe_out_dir

from . import checks, judge
from .scripted_replies import REPLAY_SUITE
from .session_close import RUBRIC_FILE, Case, load_cases

SERVED_TOOLS = ("read_instructions", "recall_context")
HOOKS = ("UserPromptSubmit",)
#: A seeded vault answers its first read before its index is ready; the recorder waits this long between reads.
WARM_SECONDS = 5
#: Key `deterministic` entries that the protocol fixes, and the answer check that each one selects.
CHECK_TYPES = {"all_of": "answer_has_all_of", "in_order": "answer_in_order"}
#: Every replay scenario carries this check: an agent that used a tool outside the script (a built-in tool, where the
#: scenario allows none) did not answer from the replayed context, and the report counts such a run as unknown.
SCRIPT_CHECK = checks.only_scripted_tools.__name__


def read_marks(case: Case) -> dict:
    """The case's marks key, or a SystemExit that names the file and what to change."""
    path = case.path.parent / "answer-keys" / f"{case.id}.marks.json"
    try:
        marks = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise SystemExit(f"{case.id}: cannot read {path}: {error}") from error
    problems = []
    if not isinstance(marks, dict):
        raise SystemExit(f"{path}: must be an object")
    if not isinstance(marks.get("reference"), str) or not marks["reference"].strip():
        problems.append("reference must be a non-empty string")
    if not isinstance(marks.get("set"), str) or not marks["set"].strip():
        problems.append("set must be a non-empty string")
    deterministic = marks.get("deterministic")
    if deterministic is not None and not (isinstance(deterministic, dict) and set(deterministic) <= set(CHECK_TYPES)):
        problems.append(f"deterministic must be null or an object with keys from {sorted(CHECK_TYPES)}")
    elif deterministic:
        # The run-time check's own reading of the groups, so a key that it would score as unknown stops here.
        problems.extend(
            f"deterministic.{name}: {checks.GROUPS_SHAPE}"
            for name, groups in deterministic.items()
            if checks.value_groups({"groups": groups}) is None
        )
    if problems:
        raise SystemExit(f"{path}: " + "; ".join(problems))
    return marks


def read_rubric(case_dir: Path) -> list[dict]:
    """The case folder's judged criteria, or a SystemExit that names the file and what to change."""
    path = case_dir / RUBRIC_FILE
    wanted = "a non-empty list of {id, text, expect} objects, the criteria the judge answers for every case"
    try:
        rubric = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise SystemExit(f"{path}: the case folder needs this file, {wanted}: {error}") from error
    if not isinstance(rubric, list) or not rubric:
        raise SystemExit(f"{path}: must be {wanted}")
    problems = [problem for problem in map(judge.criterion_problem, rubric) if problem]
    ids = [criterion["id"] for criterion in rubric if judge.criterion_problem(criterion) is None]
    if len(set(ids)) != len(ids):
        problems.append("each criterion id must be unique, because the judge answers by id")
    if problems:
        raise SystemExit(f"{path}: " + "; ".join(problems))
    return rubric


def scenario_for(case: Case, marks: dict, rubric: list[dict]) -> dict:
    if len(case.turns) != 1:
        raise SystemExit(f"{case.id}: a replay serves one turn, and the case has {len(case.turns)}")
    turn = case.turns[0]
    return {
        "id": case.id,
        "title": case.title,
        "suite": REPLAY_SUITE,
        "prompt": turn,
        "engagement": case.engagement,
        "hooks": list(HOOKS),
        "served_tools": list(SERVED_TOOLS),
        "builtin_tools": [],
        "record": {
            "seed": str(case.seed.resolve()),
            "ready_graph": True,
            "steps": [
                {
                    "as": "instructions",
                    "tool": "read_instructions",
                    "arguments": {"profile": "compact"},
                },
                {
                    "as": "context",
                    "tool": "recall_context",
                    "arguments": {"turn": turn},
                    "warm": {"seconds": WARM_SECONDS, "ignore": ["served_context_id"]},
                },
            ],
        },
        "replies": {"read_instructions": {"$shape": "instructions"}, "recall_context": {"$shape": "context"}},
        "default_reply": {"error": "this tool is not served in the replay"},
        "judge_view": "answer",
        "judge": "batch",
        "reference": marks["reference"],
        "criteria": rubric,
        "checks": [
            {"type": SCRIPT_CHECK},
            *({"type": CHECK_TYPES[name], "groups": groups} for name, groups in (marks["deterministic"] or {}).items()),
        ],
    }


def write_scenarios(case_dir: Path, out: Path) -> list[Path]:
    """`<out>/scenarios/<id>.json` for every case, and `<out>/case-sets.json` mapping each case id to its set."""
    try:
        out = refuse_unsafe_out_dir(out)
    except JourneySetupError as error:
        raise SystemExit(str(error)) from error
    rubric = read_rubric(case_dir)
    cases = load_cases("all", case_dir)
    marks = {case.id: read_marks(case) for case in cases}
    scenarios = {case.id: scenario_for(case, marks[case.id], rubric) for case in cases}
    sets = {case_id: key["set"] for case_id, key in marks.items()}
    folder = out / "scenarios"
    folder.mkdir(parents=True, exist_ok=True)
    paths = []
    for case_id, scenario in scenarios.items():
        paths.append(folder / f"{case_id}.json")
        paths[-1].write_text(json.dumps(scenario, indent=1) + "\n", encoding="utf-8")
    (out / "case-sets.json").write_text(json.dumps(sets, indent=1) + "\n", encoding="utf-8")
    return paths
