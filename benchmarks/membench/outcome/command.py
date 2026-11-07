"""``membench outcome``: run a suite on subscription actors, then report it.

``run`` plays a suite end to end and ``report`` judges it where it needs a judge and writes
the per-case report with paired intervals. A held-out run passes the gate first; a dev run
never does and every report says which it is. See ``README.md`` for the suites and the layout.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import gate, record
from .isolation import ACTOR_EFFORT, ACTOR_MODEL, REPO_ROOT
from .judging import DEFAULT_MODELS, JudgeSpec
from .workspace import RUN_ROOT

SUITES = ("clbench", "lab", "planted")
PLANTED_ARMS = ("E", "P")
#: The comparisons each suite reports when both arms ran: (treatment, baseline).
PAIRS = {
    "clbench": ("F-N", "P-N", "E-N", "EH-N", "P-F", "E-F", "EH-F", "P-E", "EH-E"),
    "lab": ("WP-S", "W-S", "WP-W"),
    "planted": ("E-P",),
}
#: CL-Bench's decision schedule: upstream's default schedule at seed 42, three independent orderings.
SCHEDULE = {"schedule": "default", "seed": 42}
HELDOUT_RUN_INDEXES = (1, 2, 3)
PANEL_RUN_INDEX = 0
#: The share of items a judge sees twice, to measure how often it repeats itself.
REJUDGE_FRACTION = 0.2


def runs_dir() -> Path:
    return RUN_ROOT / "runs"


def suite_arms(suite: str) -> tuple[str, ...]:
    if suite == "clbench":
        from .clbench.run import ARMS
    elif suite == "lab":
        from .lab.run import ARMS
    else:
        return PLANTED_ARMS
    return tuple(ARMS)


def load_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def data_split(split: str) -> str:
    """The case files a split plays: the release panel draws on the dev cases."""
    return "dev" if split == "panel" else split


def planted_cases(split: str, limit: int | None) -> list[dict]:
    return load_jsonl(RUN_ROOT / "cases" / "planted" / f"{data_split(split)}.jsonl")[:limit]


# --------------------------------------------------------------------------
# The run record.
# --------------------------------------------------------------------------


def refuse(message: str) -> SystemExit:
    return SystemExit(f"refused: {message}")


def passed_gate(suite: str, split: str) -> dict | None:
    """The held-out gate's verdict for a decision run; a dev run is never gated."""
    if split != "heldout":
        return None
    from protocol.contracts import AmendmentAcknowledgmentPendingError

    try:
        return gate.heldout_gate(suite)
    except (AmendmentAcknowledgmentPendingError, gate.HeldoutGateError) as error:
        raise refuse(f"held-out {suite} run: {error}") from error


def write_record(run_dir: Path, entry: dict) -> dict:
    """``outcome.json``: what a run is. A later call for the same run id must agree; arms and seeds add up."""
    path = run_dir / "outcome.json"
    run_dir.mkdir(parents=True, exist_ok=True)
    if path.exists():
        have = json.loads(path.read_text())
        for key in ("suite", "split", "model", "effort", "product_python", "limit"):
            if have[key] != entry[key]:
                raise refuse(
                    f"{path} records {key} {have[key]!r} and this call asks for {entry[key]!r}; "
                    "use another --run-id"
                )
        order = suite_arms(entry["suite"])
        entry = {
            **have,
            "arms": [a for a in order if a in {*have["arms"], *entry["arms"]}],
            "seeds": sorted({*(have["seeds"] or []), *(entry["seeds"] or [])}) or None,
        }
    path.write_text(json.dumps(entry, indent=2))
    return entry


def read_record(run_dir: Path) -> dict:
    """The run's record. A run that predates the command has none and counts as a dev run."""
    path = run_dir / "outcome.json"
    if path.exists():
        return json.loads(path.read_text())
    return {"split": "dev", "limit": None, "arms": None}


def git_state() -> dict:
    def git(*args: str) -> str:
        done = subprocess.run(  # noqa: S603 - fixed argv
            ["git", "-C", str(REPO_ROOT), *args], capture_output=True, text=True
        )
        return done.stdout.strip()

    return {
        "product_revision": git("rev-parse", "HEAD"),
        "product_dirty": bool(git("status", "--porcelain")),
    }


# --------------------------------------------------------------------------
# Running a suite.
# --------------------------------------------------------------------------


def clbench_cells(run_dir: Path, split: str, arms, seeds, limit, model, effort, product_python):
    from .clbench.run import CellSpec

    if split == "heldout":
        schedule, indexes = SCHEDULE, tuple(seeds or HELDOUT_RUN_INDEXES)
    elif split == "panel":
        schedule, indexes = SCHEDULE, (PANEL_RUN_INDEX,)
    elif limit:
        # A variant fixes its own question count, so a limited dev run takes upstream's plain defaults.
        schedule, indexes = {}, tuple(seeds or (None,))
    else:
        schedule, indexes = {"variant": "multi_group"}, tuple(seeds or (None,))
    return [
        CellSpec(
            run_dir=run_dir,
            arm=arm,
            **schedule,
            run_index=index,
            limit=limit,
            model=model,
            effort=effort,
            product_python=product_python,
        )
        for index in indexes
        for arm in arms
    ]


def run_clbench(run_dir, split, arms, seeds, jobs, limit, model, effort, product_python) -> None:
    from .clbench.run import run_cell

    cells = clbench_cells(run_dir, split, arms, seeds, limit, model, effort, product_python)
    todo = []
    for cell in cells:
        if (cell.dir / "evaluation.json").exists():
            print(f"{cell.dir.name}: already evaluated", flush=True)
        else:
            todo.append(cell)
    with ThreadPoolExecutor(max_workers=jobs) as pool:
        for future in [pool.submit(run_cell, cell) for cell in todo]:
            future.result()


def run_lab_suite(run_dir, split, arms, jobs, limit, model, effort, product_python) -> None:
    from .lab import prepare
    from .lab import run as lab_run

    prepared = prepare.ensure_prepared(data_split(split))
    tasks = prepare.task_ids_from(data_split(split), None)[:limit] if limit else None
    lab_run.run_lab(
        run_dir,
        prepared=prepared,
        tasks=tasks,
        arms=list(arms),
        jobs=jobs,
        model=model,
        effort=effort,
        product_python=product_python,
    )


def run_planted(run_dir, split, arms, jobs, limit, model, effort, product_python) -> None:
    from .planted.answer import answer_all
    from .planted.build import build

    cases = planted_cases(split, limit)
    distractors = load_jsonl(RUN_ROOT / "cases" / "planted" / "distractors.jsonl")

    def one(arm: str) -> None:
        arm_dir = run_dir / arm
        if not (arm_dir / "pages.json").exists():
            if arm_dir.exists():
                raise refuse(f"{arm_dir} holds an unfinished build; move it aside and run again")
            arm_dir.mkdir(parents=True)
            asyncio.run(build(cases, distractors, arm, arm_dir, product_python))
        answer_all(cases, arm_dir, model, effort, product_python)

    with ThreadPoolExecutor(max_workers=jobs) as pool:
        for future in [pool.submit(one, arm) for arm in arms]:
            future.result()


def run_suite(
    suite: str,
    run_id: str,
    *,
    split: str = "dev",
    arms: list[str] | None = None,
    seeds: list[int] | None = None,
    jobs: int = 1,
    limit: int | None = None,
    model: str = ACTOR_MODEL,
    effort: str = ACTOR_EFFORT,
    product_python: Path | None = None,
) -> Path:
    """Play ``suite``'s arms on ``split``. A held-out run passes the gate before any session starts."""
    known = suite_arms(suite)
    requested = arms or list(known)
    if set(requested) - set(known):
        raise refuse(f"--arms takes {', '.join(known)} for {suite}, not {','.join(requested)}")
    arms = [a for a in known if a in requested]
    if seeds and suite != "clbench":
        raise refuse(f"--seeds selects CL-Bench run indexes; {suite} has no seeds")
    if split == "heldout" and seeds and not set(seeds) <= set(HELDOUT_RUN_INDEXES):
        raise refuse(f"a held-out run uses run indexes {HELDOUT_RUN_INDEXES}, not {sorted(seeds)}")
    if limit and split != "dev":
        raise refuse("--limit is for dev runs; a decision run plays the whole split")
    verdict = passed_gate(suite, split)
    run_dir = runs_dir() / run_id
    write_record(
        run_dir,
        {
            "suite": suite,
            "split": split,
            "arms": arms,
            "seeds": sorted(seeds) if seeds else None,
            "limit": limit,
            "model": model,
            "effort": effort,
            "product_python": str(product_python) if product_python else None,
            "gate": verdict,
            **git_state(),
        },
    )
    if suite == "clbench":
        run_clbench(run_dir, split, arms, seeds, jobs, limit, model, effort, product_python)
    elif suite == "lab":
        run_lab_suite(run_dir, split, arms, jobs, limit, model, effort, product_python)
    else:
        run_planted(run_dir, split, arms, jobs, limit, model, effort, product_python)
    return run_dir


# --------------------------------------------------------------------------
# Reporting a suite.
# --------------------------------------------------------------------------


def present(pairs: tuple[str, ...], arms) -> list[tuple[str, str]]:
    """The suite's comparisons whose two arms both ran."""
    split = [tuple(p.split("-", 1)) for p in pairs]
    return [(a, b) for a, b in split if a in arms and b in arms]


def report_suite(
    suite: str,
    run_ids: list[str],
    spec: JudgeSpec | None,
    *,
    jobs: int = 1,
    arms: list[str] | None = None,
) -> dict:
    """The unstamped report of runs of one suite. Lab and planted judge first; CL-Bench needs no judge."""
    run_dir = runs_dir() / run_ids[0]
    have = arms or read_record(run_dir)["arms"] or list(suite_arms(suite))
    pairs = PAIRS[suite]
    if suite == "lab":
        # WG ablates the arm whose vault it copied: W in runs before WP existed, WP since.
        manifest = json.loads((run_dir / "manifest.json").read_text())
        pairs += (f"WG-{manifest.get('wg_copy_of', 'W')}",)
    wanted = present(pairs, have)
    if suite == "clbench":
        from .clbench import report

        built = report.build(run_dir, wanted)
    else:
        if spec is None:
            raise refuse(f"{suite} needs a judge: add --judge claude or --judge codex")
        judge_run = run_dir / f"judge-{spec.backend}"
        if suite == "lab":
            from .lab import judge

            judge.items(judge_run, run_ids, list(suite_arms("lab")))
        else:
            from .planted import judge

            stored = read_record(run_dir)
            judge.items(planted_cases(stored["split"], stored["limit"]), run_dir, judge_run, have)
        judge.judge(judge_run, spec, REJUDGE_FRACTION, jobs)
        built = {"suite": suite, "run_id": run_ids[0], **judge.score(judge_run, wanted)}
    built["actor"] = record.run_identity(*(runs_dir() / r for r in run_ids))
    return built


def summary(report: dict) -> list[str]:
    """One line per paired measure: what differs, by how much, with its interval."""
    lines = []
    for pair, body in report["pairs"].items():
        measures = {"": body} if "mean_difference" in body else body
        for name, m in measures.items():
            label = f"{pair} {name}".strip()
            if m.get("mean_difference") is None:
                lines.append(f"{label}: {m.get('verdict', 'unknown')}")
            else:
                lines.append(
                    f"{label}: {m['mean_difference']:+} (95% CI {m['ci95'][0]} to {m['ci95'][1]}) {m['verdict']}"
                )
    return lines


# --------------------------------------------------------------------------
# The command line.
# --------------------------------------------------------------------------


def _model_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--model", default=ACTOR_MODEL, help="the actor model (default %(default)s)"
    )
    parser.add_argument(
        "--effort", default=ACTOR_EFFORT, help="the actor effort (default %(default)s)"
    )
    parser.add_argument(
        "--product-python",
        type=Path,
        default=None,
        help="an interpreter with the Exomem release to test; default is this checkout",
    )


def _csv(text: str | None) -> list[str] | None:
    return [part for part in text.split(",") if part] if text else None


def _cmd_fetch(args: argparse.Namespace) -> int:
    from .fetch import fetch

    fetch(RUN_ROOT)
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    run_dir = run_suite(
        args.suite,
        args.run_id,
        split=args.split,
        arms=_csv(args.arms),
        seeds=[int(s) for s in _csv(args.seeds) or []] or None,
        jobs=args.jobs,
        limit=args.limit,
        model=args.model,
        effort=args.effort,
        product_python=args.product_python,
    )
    print(
        f"{args.suite} {args.split} run in {run_dir}; next: membench outcome report {args.suite} --run-id {args.run_id}"
    )
    return 0


def _cmd_report(args: argparse.Namespace) -> int:
    run_dir = runs_dir() / args.run_id
    if not run_dir.is_dir():
        raise refuse(f"no run at {run_dir}")
    stored = read_record(run_dir)
    if stored.get("suite", args.suite) != args.suite:
        raise refuse(f"{run_dir} is a {stored['suite']} run, not {args.suite}")
    spec = (
        JudgeSpec.of(args.judge, args.judge_model, args.judge_effort, args.judge_exe)
        if args.suite != "clbench"
        else None
    )
    built = report_suite(args.suite, [args.run_id], spec, jobs=args.jobs)
    built["run"] = {
        k: stored.get(k) for k in ("model", "effort", "product_python", "arms", "seeds", "gate")
    }
    stamped = gate.stamp(built, stored["split"])
    target = run_dir / (f"report-{spec.backend}.json" if spec else "report.json")
    target.write_text(json.dumps(stamped, indent=2))
    print(f"{stamped['label']} ({stamped['evidence']} evidence, {stamped['split']} split)")
    print("\n".join(summary(stamped)))
    print(f"report: {target}")
    return 0


def _cmd_panel(args: argparse.Namespace) -> int:
    from .panel import run_panel

    spec = JudgeSpec.of(args.judge, args.judge_model, args.judge_effort, args.judge_exe)
    written = run_panel(
        args.run_id,
        args.version,
        spec,
        jobs=args.jobs,
        model=args.model,
        effort=args.effort,
        product_python=args.product_python,
        controls=args.controls,
    )
    print("\n".join(str(path) for path in written))
    return 0


def register(sub) -> None:
    """Add the ``outcome`` group to the ``membench`` command line."""
    group = sub.add_parser("outcome", help="outcome evaluations on subscription actors")
    commands = group.add_subparsers(dest="outcome_command", required=True)

    p_fetch = commands.add_parser(
        "fetch", help="clone the pinned upstreams into the run root and prepare their environments"
    )
    p_fetch.set_defaults(func=_cmd_fetch)

    p_run = commands.add_parser("run", help="play a suite's arms end to end")
    p_run.add_argument("suite", choices=SUITES)
    p_run.add_argument("--run-id", required=True)
    p_run.add_argument(
        "--split",
        choices=("dev", "heldout"),
        default="dev",
        help="dev is an instrument run; heldout is a decision run and passes the gate first",
    )
    p_run.add_argument("--arms", help="comma-separated arm codes; default all of the suite's arms")
    p_run.add_argument("--seeds", help="CL-Bench only: comma-separated run indexes")
    p_run.add_argument("--jobs", type=int, default=1, help="parallel cells, tasks or arms")
    p_run.add_argument(
        "--limit", type=int, help="dev only: play the first N questions, tasks or cases"
    )
    _model_args(p_run)
    p_run.set_defaults(func=_cmd_run)

    p_report = commands.add_parser(
        "report", help="judge where the suite needs a judge and write the per-case report"
    )
    p_report.add_argument("suite", choices=SUITES)
    p_report.add_argument("--run-id", required=True)
    p_report.add_argument("--jobs", type=int, default=1, help="parallel judge calls")
    _judge_args(p_report)
    p_report.set_defaults(func=_cmd_report)

    p_panel = commands.add_parser(
        "panel",
        help="run the release panel and write docs/benchmarks/outcome/<version>.json and .md",
    )
    p_panel.add_argument("--run-id", required=True)
    p_panel.add_argument("--version", required=True, help="the Exomem release, for example 1.4.0")
    p_panel.add_argument("--jobs", type=int, default=1)
    p_panel.add_argument(
        "--controls",
        action="store_true",
        help="also run the control arms; they run anyway when the actor differs from the previous panel",
    )
    _model_args(p_panel)
    _judge_args(p_panel)
    p_panel.set_defaults(func=_cmd_panel)


def _judge_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--judge",
        choices=sorted(DEFAULT_MODELS),
        default="claude",
        help="the judge backend for lab and planted (default %(default)s)",
    )
    parser.add_argument("--judge-model", help="default is the backend's strongest model")
    parser.add_argument("--judge-effort")
    parser.add_argument("--judge-exe", help="path to the judge CLI, when not on PATH")
