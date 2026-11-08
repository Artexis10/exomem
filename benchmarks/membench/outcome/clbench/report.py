"""Report a CL-Bench run: per-arm scores, paired differences with intervals, units and cost.

Every number is recomputed from the run's ``outcomes.jsonl`` files. A question that an arm did
not reach is missing, never a zero. A run (one ``run_index``, all arms) with a harness fault that
survived the retries is void for every arm, because memory carries across its questions; voided
runs leave every figure, and the report lists them. A held-out decision reads as incomplete, and
decides nothing, while a scheduled cell has not run to its end. Pairs are formed question by question inside
each ``run_index``. The differences of one question are averaged over the runs before the
interval, so a question counts once: the interval is a bootstrap over questions, and the report
also gives each run's mean difference and the post-migration questions on their own.

``membench outcome report clbench`` drives this module.
"""

from __future__ import annotations

import json
import statistics
from collections import defaultdict
from pathlib import Path

from ..faults import HELDOUT_RUN_INDEXES, REPLACEMENT_RUN, books, incomplete_why, replayed
from ..stats import NOT_WORSE_BOUND, paired_interval, usage_totals
from ..workspace import pin

BOOTSTRAP_SEED = 20261107
#: Upstream's own list of the questions it asks after the schema migration.
POST_MIGRATION_FILE = "data/database_exploration/questions_post_drift.json"

#: What each comparison is for (PREREGISTRATION section 7, sequence 10): the decision reads the PP
#: pairs, P measures the product gap that a capture change must close, and the rest is description.
PAIR_ROLES = {
    "PP-N": "read by the decision",
    "PP-F": "read by the decision",
    "P-N": "product gap, description only",
    "P-F": "product gap, description only",
}

Rows = dict[tuple[int | None, str], dict]


def post_migration_ids() -> set[str] | None:
    """The questions upstream asks after its schema migration, read from its data; None when absent."""
    path = pin("clbench").checkout() / POST_MIGRATION_FILE
    if not path.is_file():
        return None
    return {str(q["question_id"]) for q in json.loads(path.read_text())}


def load_arms(run_dir: Path) -> dict[str, Rows]:
    """``arm -> {(run_index, question_id): row}``; the arm and run_index come from each manifest."""
    arms: dict[str, Rows] = defaultdict(dict)
    for cell in sorted(run_dir.iterdir()):
        if not (cell / "outcomes.jsonl").exists():
            continue
        manifest = json.loads((cell / "manifest.json").read_text())
        run_index = manifest["task"]["run_index"]
        for line in (cell / "outcomes.jsonl").read_text().splitlines():
            row = json.loads(line)
            arms[manifest["arm"]][(run_index, row["log"]["question_id"])] = row
    return dict(arms)


def cell_states(run_dir: Path) -> dict[tuple[str, int | None], dict]:
    """``(arm, run_index) -> state`` of every cell the run started: whether it was evaluated, and how far it got."""
    cells = {}
    for cell in sorted(run_dir.iterdir()):
        if not (cell / "manifest.json").exists():
            continue
        manifest = json.loads((cell / "manifest.json").read_text())
        outcomes = cell / "outcomes.jsonl"
        cells[(manifest["arm"], manifest["task"]["run_index"])] = {
            "evaluated": (cell / "evaluation.json").exists(),
            "questions": len(outcomes.read_text().splitlines()) if outcomes.exists() else 0,
            "instances": manifest["task"]["instances"],
        }
    return cells


def unfinished_cells(
    cells: dict, arms: list[str], counted: list[int | None], voided: set[int | None]
) -> list[str]:
    """The cells the decision needs that did not run to their end. A voided run needs none: it is void."""
    unfinished = []
    for run_index in (i for i in counted if i not in voided):
        for arm in arms:
            name = f"{arm} run {run_index}"
            cell = cells.get((arm, run_index))
            if cell is None:
                unfinished.append(f"{name} was not started")
            elif not cell["evaluated"]:
                unfinished.append(
                    f"{name} ended after {cell['questions']} of {cell['instances']} questions, with no evaluation.json"
                )
            elif cell["questions"] != cell["instances"]:
                unfinished.append(
                    f"{name} has {cell['questions']} of {cell['instances']} questions"
                )
    return unfinished


def instances_of(run_dir: Path) -> int | None:
    """Questions per run, from any cell's manifest; None when the run has no cell."""
    for manifest in sorted(run_dir.glob("*/manifest.json")):
        return json.loads(manifest.read_text())["task"]["instances"]
    return None


def voided_runs(arms: dict[str, Rows]) -> dict[int | None, list[dict]]:
    """``run_index -> harness faults``: every run in which any arm's question ended in one."""
    voided: dict[int | None, list[dict]] = defaultdict(list)
    for arm, rows in arms.items():
        for (run_index, question), row in rows.items():
            if faults := row["log"].get("harness_faults"):
                voided[run_index].append({"arm": arm, "question_id": question, "faults": faults})
    return dict(sorted(voided.items(), key=lambda kv: (kv[0] is None, kv[0] or 0)))


def runs_state(arms: dict[str, Rows], scheduled: list[int | None], *, heldout: bool) -> dict:
    """Which runs count. A held-out run voided by a harness fault is replaced once, by ``run_index`` 4.

    A second voided run voids the decision. Outside a held-out report there is no replacement and no decision.
    """
    played = sorted(
        {k[0] for rows in arms.values() for k in rows}, key=lambda i: (i is None, i or 0)
    )
    voided = voided_runs(arms)
    valid = [i for i in played if i not in voided]
    state: dict = {
        "scheduled": scheduled,
        "played": played,
        "voided": {str(i): faults for i, faults in voided.items()},
        "valid": valid,
        # The runs whose units the report counts: every scheduled run, voided ones included (their
        # units are invalid), and the replacement once it is played.
        "counted": sorted({*scheduled, *played}, key=lambda i: (i is None, i or 0)),
        "replacement": None,
        "decision_void": False,
        "void_reason": None,
        "incomplete": None,
    }
    if not heldout:
        return state
    base = [i for i in scheduled if i != REPLACEMENT_RUN]
    voided_base = [i for i in base if i in voided]
    state["valid"] = [i for i in base if i in played and i not in voided]
    state["counted"] = list(base)
    if len(voided) >= 2:
        state["decision_void"] = True
        state["void_reason"] = f"{len(voided)} runs are voided by harness faults: {sorted(voided)}"
    elif voided_base:
        if REPLACEMENT_RUN in played:
            state["replacement"] = {"run_index": REPLACEMENT_RUN, "replaces": voided_base[0]}
            state["valid"] = [*state["valid"], REPLACEMENT_RUN]
            state["counted"] = [*base, REPLACEMENT_RUN]
        else:
            state["incomplete"] = (
                f"run {voided_base[0]} is voided; run_index {REPLACEMENT_RUN} replaces it once"
            )
    elif REPLACEMENT_RUN in played:
        # No run needed replacing, so the extra run is not part of the decision.
        state["valid"] = [i for i in state["valid"] if i != REPLACEMENT_RUN]
    missing = [i for i in base if i not in played]
    if missing and not state["incomplete"]:
        state["incomplete"] = f"runs {missing} were not played"
    return state


def reached(rows: Rows, runs: set[int | None]) -> Rows:
    """The rows of valid runs where the question produced an upstream outcome."""
    return {key: row for key, row in rows.items() if row["outcome"] and key[0] in runs}


def unit(run_index: int | None, position: int) -> str:
    """A unit is a question by its position in the run: a question not yet asked has no id to name."""
    return f"r{run_index}:q{position:02d}"


def arm_units(rows: Rows, runs: dict, voided: set[int | None], instances: int | None) -> dict:
    """The arm's questions in the runs the report counts: valid, invalid (the run is void) or missing."""
    if instances is None:
        return books(None, set(), set())
    positions = range(instances)
    scheduled = {unit(r, i) for r in runs["counted"] for i in positions}
    valid = {
        unit(k[0], row["log"]["index"]) for k, row in reached(rows, set(runs["valid"])).items()
    }
    invalid = {unit(r, i) for r in runs["counted"] if r in voided for i in positions}
    return books(scheduled, valid, invalid)


def arm_summary(rows: Rows, runs: dict, voided: set[int | None], instances: int | None) -> dict:
    """One arm: its scored questions in valid runs, its units, and everything it spent."""
    valid_runs = set(runs["valid"])
    scored = reached(rows, valid_runs)
    outcomes = [r["outcome"] for r in scored.values()]
    rewards = [o["reward"] for o in outcomes]
    logs = [r["log"] for r in rows.values()]
    tools: dict[str, int] = {}
    injected: dict[str, int] = {}
    denied: dict[str, int] = {}
    for log in logs:
        for k, v in log["tool_uses"].items():
            tools[k] = tools.get(k, 0) + v
        for tool in log.get("permission_denials", []):
            denied[tool] = denied.get(tool, 0) + 1
        for turn in log.get("hook_chars", []):
            for event, chars in turn.items():
                injected[event] = injected.get(event, 0) + chars
    return {
        "units": arm_units(rows, runs, voided, instances),
        "scored_questions": len(scored),
        "mean_reward": round(statistics.mean(rewards), 4) if rewards else None,
        "accuracy": round(sum(o["success"] for o in outcomes) / len(outcomes), 4)
        if outcomes
        else None,
        "mean_queries": round(statistics.mean(o["metadata"]["num_queries"] for o in outcomes), 2)
        if outcomes
        else None,
        # Everything the arm spent, including questions of voided runs.
        "spent": usage_totals(logs),
        "timeouts": sum(log["timeouts"] for log in logs),
        "schema_repairs": sum(log["repairs"] for log in logs),
        "retries": sum(len(log.get("retries", [])) for log in logs),
        "tool_uses": dict(sorted(tools.items())),
        # Characters the shipped hook added to the context, by hook event; empty where no hook ran.
        "hook_chars": dict(sorted(injected.items())),
        # Tools the CLI refused for want of permission; any entry means the arm ran crippled.
        "permission_denials": dict(sorted(denied.items())),
    }


def _difference(a: Rows, b: Rows, only: set[str] | None) -> dict:
    """The paired block of two arms' reached rows, over all questions or only the ones in ``only``."""
    cells = sorted(
        (k for k in set(a) & set(b) if only is None or k[1] in only),
        key=lambda k: (k[0] or 0, len(k[1]), k[1]),
    )
    by_question: dict[str, list[float]] = defaultdict(list)
    by_run: dict[int | None, list[float]] = defaultdict(list)
    for key in cells:
        diff = a[key]["outcome"]["reward"] - b[key]["outcome"]["reward"]
        by_question[key[1]].append(diff)
        by_run[key[0]].append(diff)
    diffs = [
        statistics.mean(v)
        for _q, v in sorted(by_question.items(), key=lambda kv: (len(kv[0]), kv[0]))
    ]
    return {
        **paired_interval(diffs, seed=BOOTSTRAP_SEED, unit="question"),
        # Memory carries within a run, so each run's own mean must agree with the pooled one.
        "per_run_mean_difference": [
            {
                "run_index": run,
                "paired_questions": len(v),
                "mean_difference": round(statistics.mean(v), 4),
            }
            for run, v in sorted(by_run.items(), key=lambda kv: (kv[0] is None, kv[0] or 0))
        ],
        "wins_losses_ties": [
            sum(d > 0 for d in diffs),
            sum(d < 0 for d in diffs),
            sum(d == 0 for d in diffs),
        ],
    }


def paired(a_rows: Rows, b_rows: Rows, runs: set[int | None], post: set[str] | None) -> dict:
    a, b = reached(a_rows, runs), reached(b_rows, runs)
    block = _difference(a, b, None)
    block["missing"] = sorted(
        f"r{k[0]}:{k[1]}" if k[0] is not None else k[1] for k in set(a) ^ set(b)
    )
    # The questions upstream asks after its schema migration are unseen in development: report them apart.
    block["post_migration"] = (
        _difference(a, b, post)
        if post is not None
        else {"verdict": "unknown: upstream's post-migration question list is not available"}
    )
    return block


def per_question(rows: Rows, runs: set[int | None]) -> dict[str, float]:
    """Each question's reward, averaged over the valid ``run_index`` values the arm played it in."""
    by_question: dict[str, list[float]] = defaultdict(list)
    for (_run_index, question), row in reached(rows, runs).items():
        by_question[question].append(row["outcome"]["reward"])
    return {q: round(statistics.mean(v), 4) for q, v in sorted(by_question.items())}


def contains(interval: list[float] | None, bound: float) -> bool | None:
    """Whether the interval holds the bound its rule tests; None when there is no interval."""
    return None if interval is None else interval[0] <= bound <= interval[1]


def decision(pairs: dict, runs: dict, arms: dict[str, dict]) -> dict:
    """PREREGISTRATION §7, sequence 10: the sequence-9 rules read PP; each reads the lower bound and every run's mean.

    ``None`` means unknown. The decision stays incomplete while a rule's interval contains its bound and
    an ordering is left to play: it is complete once the orderings run out or no interval holds its bound.
    """
    played = {"run_indexes_played": runs["played"]}
    if runs["decision_void"]:
        return {"status": "void", "why": runs["void_reason"], **played}
    why = [*([runs["incomplete"]] if runs["incomplete"] else []), *runs["unfinished"]]
    if why := [*why, *incomplete_why([], arms)]:
        return {"status": "incomplete", "why": why, **played}

    def read(pair: str):
        body = pairs.get(pair)
        if not body or body.get("ci95") is None:
            return None, None
        every_run = all(r["mean_difference"] > 0 for r in body["per_run_mean_difference"])
        return body["ci95"], every_run

    helps_ci, helps_runs = read("PP-N")
    folder_ci, folder_runs = read("PP-F")
    rules = {
        "helps": {
            "rule": "lower bound of PP-N above 0 and every run's mean PP-N above 0",
            "result": None if helps_ci is None else helps_ci[0] > 0 and helps_runs,
            "interval_contains_bound": contains(helps_ci, 0),
        },
        "not_worse_than_folder": {
            "rule": f"lower bound of PP-F above {NOT_WORSE_BOUND}",
            "result": None if folder_ci is None else folder_ci[0] > NOT_WORSE_BOUND,
            "interval_contains_bound": contains(folder_ci, NOT_WORSE_BOUND),
        },
        "beats_folder": {
            "rule": "lower bound of PP-F above 0 and every run's mean PP-F above 0",
            "result": None if folder_ci is None else folder_ci[0] > 0 and folder_runs,
            "interval_contains_bound": contains(folder_ci, 0),
        },
    }
    undecided = [name for name, rule in rules.items() if rule["interval_contains_bound"]]
    left = [i for i in HELDOUT_RUN_INDEXES if i not in runs["played"]]
    if undecided and left:
        # The figures stay in the report, but a one-ordering result with an interval on its bound decides nothing yet.
        why = (
            f"the protocol calls for run_index {left[0]}: the interval of "
            f"{', '.join(undecided)} contains its bound"
        )
        return {"status": "incomplete", "why": [why], **played, **rules}
    return {"status": "complete", **played, **rules}


def build(
    run_dir: Path,
    pairs: list[tuple[str, str]],
    *,
    scheduled: list[int | None] | None = None,
    heldout: bool = False,
    arms: list[str] | None = None,
) -> dict:
    """The run's report.

    ``scheduled`` lists the ``run_index`` values the run planned (None for a plain run) and ``arms``
    the arms it planned; each defaults to what the run directory holds.
    """
    rows_by_arm = load_arms(run_dir)
    scheduled = scheduled or [None]
    runs = runs_state(rows_by_arm, scheduled, heldout=heldout)
    runs["replayed"] = replayed(run_dir, "cell")
    voided = set(voided_runs(rows_by_arm))
    runs["unfinished"] = (
        unfinished_cells(cell_states(run_dir), arms or sorted(rows_by_arm), runs["counted"], voided)
        if heldout
        else []
    )
    valid = set(runs["valid"])
    instances = instances_of(run_dir)
    post = post_migration_ids()
    cases: dict[str, dict[str, float]] = defaultdict(dict)
    for arm, rows in rows_by_arm.items():
        for question, reward in per_question(rows, valid).items():
            cases[question][arm] = reward
    pair_bodies = {
        f"{a}-{b}": {
            **paired(rows_by_arm[a], rows_by_arm[b], valid, post),
            **({"role": PAIR_ROLES[f"{a}-{b}"]} if f"{a}-{b}" in PAIR_ROLES else {}),
        }
        for a, b in pairs
        if a in rows_by_arm and b in rows_by_arm
    }
    summaries = {
        name: arm_summary(rows, runs, voided, instances) for name, rows in rows_by_arm.items()
    }
    report = {
        "suite": "clbench",
        "run_id": run_dir.name,
        "measure": "mean reward per question (upstream scoring), 0 to 1",
        "interval": "percentile bootstrap over questions, 10,000 resamples; each question's differences are averaged over runs first",
        "runs": runs,
        "arms": summaries,
        "pairs": pair_bodies,
        "cases": dict(sorted(cases.items(), key=lambda kv: (len(kv[0]), kv[0]))),
    }
    if heldout:
        report["decision"] = decision(pair_bodies, runs, summaries)
    return report
