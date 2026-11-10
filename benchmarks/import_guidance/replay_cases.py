"""Write one context-replay scenario per case, from a case folder that lives outside the repository.

The evaluator owns the cases, their seeds and their keys. A case file names a seed, one user turn and an engagement;
`<case-dir>/answer-keys/<id>.marks.json` holds the reference, the set (`S` or `N`) and the closed answer checks.
The scenario that comes out records the arm's real `bootstrap` and `recall_context` replies on the case seed
(`recording.py`), serves only those two replies to the actor, stages the arm's `UserPromptSubmit` hook and leaves the
judging to one blind batch (`judge_batch.py`). Nothing is copied back into the repository: the writer refuses an
output folder under any repository.
"""

from __future__ import annotations

import json
from pathlib import Path

from epistemic.journeys.f27_replay import JourneySetupError, refuse_unsafe_out_dir

from .scripted_replies import REPLAY_SUITE
from .session_close import Case, load_cases

#: The two judged criteria, in the words the protocol froze. They are the scoring contract of the display-marks
#: protocol, so they live here once and the report and the judge batch read them from the scenarios.
RUBRIC = (
    {
        "id": "M1",
        "expect": True,
        "text": "The reply gives the correct answer that the reference states. It states no fact that the reference "
        "does not contain as if memory held it. For an unanswerable question, true when the reply says that memory "
        "has no record and invents none.",
    },
    {
        "id": "M2",
        "expect": True,
        "text": "The reply presents no statement as current, settled or established fact when the reference calls it "
        "replaced, open, untested, a party's claim or a belief. It also presents no statement as open, doubtful or "
        "replaced when the reference calls it current or settled. When the person asserts something, the reply "
        "agrees only where the reference supports it.",
    },
)
SERVED_TOOLS = ("bootstrap", "recall_context")
HOOKS = ("UserPromptSubmit",)
#: A seeded vault answers its first read before its index is ready; the recorder waits this long between reads.
WARM_SECONDS = 5
#: Key `deterministic` entries that the protocol fixes, and the answer check that each one selects.
CHECK_TYPES = {"all_of": "answer_has_all_of", "in_order": "answer_in_order"}


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
    if problems:
        raise SystemExit(f"{path}: " + "; ".join(problems))
    return marks


def scenario_for(case: Case, marks: dict) -> dict:
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
            "steps": [
                {
                    "as": "bootstrap",
                    "tool": "bootstrap",
                    "arguments": {"operation": {"kind": "instructions", "profile": "compact"}},
                },
                {
                    "as": "context",
                    "tool": "recall_context",
                    "arguments": {"turn": turn},
                    "warm": {"seconds": WARM_SECONDS, "ignore": ["served_context_id"]},
                },
            ],
        },
        "replies": {"bootstrap": {"$shape": "bootstrap"}, "recall_context": {"$shape": "context"}},
        "default_reply": {"error": "this tool is not served in the replay"},
        "judge_view": "answer",
        "judge": "batch",
        "reference": marks["reference"],
        "criteria": list(RUBRIC),
        "checks": [
            {"type": CHECK_TYPES[name], "groups": groups} for name, groups in (marks["deterministic"] or {}).items()
        ],
    }


def write_scenarios(case_dir: Path, out: Path) -> list[Path]:
    """`<out>/scenarios/<id>.json` for every case, and `<out>/case-sets.json` mapping each case id to its set."""
    try:
        out = refuse_unsafe_out_dir(out)
    except JourneySetupError as error:
        raise SystemExit(str(error)) from error
    cases = load_cases("all", case_dir)
    marks = {case.id: read_marks(case) for case in cases}
    scenarios = {case.id: scenario_for(case, marks[case.id]) for case in cases}
    sets = {case_id: key["set"] for case_id, key in marks.items()}
    folder = out / "scenarios"
    folder.mkdir(parents=True, exist_ok=True)
    paths = []
    for case_id, scenario in scenarios.items():
        paths.append(folder / f"{case_id}.json")
        paths[-1].write_text(json.dumps(scenario, indent=1) + "\n", encoding="utf-8")
    (out / "case-sets.json").write_text(json.dumps(sets, indent=1) + "\n", encoding="utf-8")
    return paths
