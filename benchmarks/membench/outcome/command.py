"""``membench outcome``: run a suite on subscription actors, then report it.

``run`` plays a suite end to end and ``report`` judges it where it needs a judge and writes
the per-case report with paired intervals. A held-out run passes the gate first; a dev run
never does and every report says which it is. See ``README.md`` for the suites and the layout.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from functools import partial
from pathlib import Path

from . import billing, gate, record
from .faults import HELDOUT_RUN_INDEXES, REPLACEMENT_RUN, LimitRejected, park, replayed, run_pool
from .isolation import ACTOR_EFFORT, ACTOR_MODEL
from .judging import DECISION_JUDGE, DEFAULT_MODELS, JudgeSpec, read_rows, write_sample
from .workspace import RUN_ROOT, require_pinned_checkout

SUITES = ("clbench", "lab", "planted", "epistemic")
PLANTED_ARMS = ("E", "P")
#: The comparisons each suite reports when both arms ran: (treatment, baseline).
PAIRS = {
    "clbench": (
        "F-N",
        "PP-N",
        "P-N",
        "E-N",
        "EH-N",
        "PP-F",
        "P-F",
        "E-F",
        "EH-F",
        "P-E",
        "EH-E",
    ),
    "lab": ("WP-S", "W-S", "WP-W"),
    "planted": ("E-P",),
    "epistemic": ("P-F", "P-N", "P-E"),
}
SPLITS = ("dev", "heldout", "panel")
#: CL-Bench's decision schedule: upstream's default schedule at seed 42. Development rounds and the
#: panel play the ``multi_group`` variant only, so the post-migration questions of this schedule
#: stay unseen until the decision run.
SCHEDULE = {"schedule": "default", "seed": 42}
#: The arms that decide f35. E and EH are ablations: a held-out run refuses them.
DECISION_ARMS = ("N", "F", "PP", "P")
#: The share of items a judge sees twice, to measure how often it repeats itself.
REJUDGE_FRACTION = 0.2


def runs_dir() -> Path:
    return RUN_ROOT / "runs"


def suite_arms(suite: str) -> tuple[str, ...]:
    if suite == "clbench":
        from .clbench.run import ARMS
    elif suite == "lab":
        from .lab.run import ARMS
    elif suite == "epistemic":
        from .epistemic.run import EPISTEMIC_ARMS as ARMS
    else:
        return PLANTED_ARMS
    return tuple(ARMS)


def data_split(split: str) -> str:
    """The case files a split plays: the release panel draws on the dev cases."""
    return "dev" if split == "panel" else split


def planted_cases(split: str, limit: int | None) -> list[dict]:
    return read_rows(RUN_ROOT / "cases" / "planted" / f"{data_split(split)}.jsonl")[:limit]


# --------------------------------------------------------------------------
# The run record.
# --------------------------------------------------------------------------


def refuse(message: str) -> SystemExit:
    return SystemExit(f"refused: {message}")


@contextmanager
def waits_for_room() -> Iterator[None]:
    """A rejected subscription limit ends the command with one line; the run then reads as incomplete."""
    try:
        yield
    except LimitRejected as stop:
        raise refuse(
            f"{stop}. The run stopped at once and waits for room as an incomplete run: "
            "run the same command again when there is room"
        ) from stop


def passed_gate(suite: str, split: str) -> dict | None:
    """The held-out gate's verdict for a decision run; a dev run is never gated."""
    if split != "heldout":
        return None
    if suite not in gate.FAMILIES:
        raise refuse(
            f"held-out {suite} run: {suite} is not registered in the preregistration, so no gate "
            "can pass it. Run the dev split until its amendment registers it"
        )
    from protocol.contracts import AmendmentAcknowledgmentPendingError

    if suite in ("clbench", "lab"):
        # The questions, documents and criteria are the pinned upstream's files: edited ones are not.
        require_pinned_checkout(suite, unedited=True)

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
        record.require_same_run(
            path,
            have,
            entry,
            ("suite", "split", "model", "effort", "limit", "billing", *record.IDENTITY_KEYS),
        )
        order = suite_arms(entry["suite"])
        entry = {
            **have,
            "arms": [a for a in order if a in {*have["arms"], *entry["arms"]}],
            "seeds": sorted({*(have["seeds"] or []), *(entry["seeds"] or [])}) or None,
            # f38 plays one project per call under --project: its case checks add up like its arms.
            **(
                {"case_check": {**have.get("case_check", {}), **entry["case_check"]}}
                if "case_check" in entry
                else {}
            ),
        }
    path.write_text(json.dumps(entry, indent=2))
    return entry


def read_record(run_dir: Path) -> dict:
    """The run's record. A run that predates the command has none and counts as a dev run."""
    path = run_dir / "outcome.json"
    if path.exists():
        return json.loads(path.read_text())
    return {"split": "dev", "limit": None, "arms": None}


# --------------------------------------------------------------------------
# Running a suite.
# --------------------------------------------------------------------------


def clbench_indexes(split: str, seeds: list[int] | None) -> tuple[int | None, ...]:
    """The ``run_index`` values a split plays: the decision's first ordering unless ``seeds`` adds more, or one plain run."""
    if split == "heldout":
        return tuple(sorted(set(seeds or HELDOUT_RUN_INDEXES[:1])))
    return tuple(seeds or (None,))


def require_orderings_in_order(seeds: list[int]) -> None:
    """A held-out run adds orderings in their order: 1, then 2, then 3. Index 4 is the replacement, once."""
    orderings = sorted(set(seeds) - {REPLACEMENT_RUN})
    if orderings != list(HELDOUT_RUN_INDEXES[: len(orderings)]):
        raise refuse(
            f"a held-out run adds orderings in order, so --seeds takes 1, 1,2 or 1,2,3, "
            f"and {REPLACEMENT_RUN} once as the replacement for a voided one, not {','.join(map(str, seeds))}"
        )


def clbench_cells(run_dir: Path, split: str, arms, seeds, limit, model, effort):
    from .clbench.run import CellSpec

    if split == "heldout":
        schedule = SCHEDULE
    elif limit:
        # A variant fixes its own question count, so a limited dev run takes upstream's plain defaults.
        schedule = {}
    else:
        schedule = {"variant": "multi_group"}
    return [
        CellSpec(
            run_dir=run_dir,
            arm=arm,
            **schedule,
            run_index=index,
            limit=limit,
            model=model,
            effort=effort,
        )
        for index in clbench_indexes(split, seeds)
        for arm in arms
    ]


def require_replacement(run_dir: Path) -> None:
    """``run_index`` 4 is the one replacement for a held-out run that a harness fault voided."""
    from .clbench import report

    voided = [i for i in report.voided_runs(report.load_arms(run_dir)) if i in HELDOUT_RUN_INDEXES]
    if len(voided) != 1:
        raise refuse(
            f"run_index {REPLACEMENT_RUN} replaces exactly one voided held-out run; "
            f"{run_dir.name} has {len(voided)} voided ({voided})"
        )


def run_clbench(run_dir, split, arms, seeds, jobs, limit, model, effort) -> None:
    from .clbench.run import run_cell

    if split == "heldout" and REPLACEMENT_RUN in clbench_indexes(split, seeds):
        require_replacement(run_dir)
    from .clbench import report

    def play(cell) -> None:
        # A voided run index is void for every arm, so its remaining cells would spend quota on nothing.
        if cell.run_index in report.voided_runs(report.load_arms(run_dir)):
            print(f"{cell.dir.name}: skipped, run {cell.run_index} is voided", flush=True)
            return
        if cell.dir.exists():
            # Started and never evaluated: upstream's question sequence cannot resume, so the cell
            # replays from its first question. The old cell stays under ``unfinished/`` as evidence.
            park(run_dir, cell.dir)
        run_cell(cell)

    todo = []
    for cell in clbench_cells(run_dir, split, arms, seeds, limit, model, effort):
        if (cell.dir / "evaluation.json").exists():
            print(f"{cell.dir.name}: already evaluated", flush=True)
        else:
            todo.append(cell)
    run_pool(jobs, [partial(play, cell) for cell in todo])


def run_lab_suite(run_dir, split, arms, jobs, limit, model, effort) -> None:
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
    )


def run_planted(run_dir, split, arms, jobs, limit, model, effort) -> None:
    from .planted.answer import answer_all
    from .planted.build import build, require_identical_text

    cases = planted_cases(split, limit)
    distractors = read_rows(RUN_ROOT / "cases" / "planted" / "distractors.jsonl")

    def built(arm: str) -> None:
        arm_dir = run_dir / arm
        if not (arm_dir / "built").is_dir():
            if arm_dir.exists():
                raise refuse(f"{arm_dir} holds an unfinished build; move it aside and run again")
            arm_dir.mkdir(parents=True)
            asyncio.run(build(cases, distractors, arm, arm_dir))

    # Both arms are always built: the identical-text proof needs the pair, and a build costs no session.
    with ThreadPoolExecutor(max_workers=jobs) as pool:
        list(pool.map(built, PLANTED_ARMS))
    require_identical_text(run_dir)
    run_pool(jobs, [partial(answer_all, cases, run_dir / arm, model, effort) for arm in arms])


def require_epistemic_options(
    split: str, arms: list[str], seeds, limit, window: int, history: str | None
) -> None:
    """The f38 options that have one meaning: windows are numbered from 1, and the solvable gate is arm N on dev."""
    if split == "panel":
        raise refuse("the release panel has no epistemic suite")
    if seeds or limit:
        raise refuse("--seeds and --limit do not apply to epistemic: a run plays whole projects")
    if window < 1:
        raise refuse(f"--window takes a number from 1, not {window}")
    if history and (arms != ["N"] or split != "dev"):
        raise refuse(
            "--history full is the solvable gate: arm N on the dev split, so add --arms N and keep --split dev"
        )


def run_epistemic(
    run_dir: Path, split: str, arms, jobs, window, history, projects, model, effort
) -> None:
    from .epistemic import run as epistemic_run

    played = epistemic_run.Window(run_dir, RUN_ROOT, window, split, model, effort, None)
    if history:
        epistemic_run.play_history(played, projects, jobs)
    else:
        epistemic_run.play_window(played, projects, arms, jobs)


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
    window: int | None = None,
    project: str | None = None,
    history: str | None = None,
) -> Path:
    """Play ``suite``'s arms on ``split``. A held-out run passes the gate before any session starts."""
    if suite != "epistemic" and (window or project or history):
        raise refuse("--window, --project and --history belong to the epistemic suite")
    if split not in SPLITS:
        raise refuse(f"--split takes {', '.join(SPLITS)}, not {split!r}")
    if split == "heldout" and (model, effort) != (ACTOR_MODEL, ACTOR_EFFORT):
        raise refuse(
            f"a held-out run uses the frozen actor {ACTOR_MODEL} at {ACTOR_EFFORT} effort, "
            f"not {model} at {effort}"
        )
    if split == "panel" and (seeds or limit):
        raise refuse("the panel plays its fixed slice of development cases: no --seeds or --limit")
    known = suite_arms(suite)
    decides = DECISION_ARMS if split == "heldout" and suite == "clbench" else known
    default = decides
    if suite == "epistemic":
        from .epistemic.cases import GOLD_ARM

        # G reads a key's contest set, which the hand-written projects lack: it plays only when asked for.
        default = tuple(a for a in known if a != GOLD_ARM)
    requested = arms or list(default)
    if set(requested) - set(known):
        raise refuse(f"--arms takes {', '.join(known)} for {suite}, not {','.join(requested)}")
    if set(requested) - set(decides):
        raise refuse(
            f"a held-out CL-Bench run plays {', '.join(decides)}; E and EH are ablations and never decide"
        )
    arms = [a for a in known if a in requested]
    if seeds and suite != "clbench":
        raise refuse(f"--seeds selects CL-Bench run indexes; {suite} has no seeds")
    if split == "heldout" and seeds:
        require_orderings_in_order(seeds)
    if limit and split != "dev":
        raise refuse("--limit is for dev runs; a decision run plays the whole split")
    projects = None
    if suite == "epistemic":
        window = window or 1
        require_epistemic_options(split, arms, seeds, limit, window, history)
    verdict = passed_gate(suite, split)
    if suite == "epistemic":
        # The case check runs before the record or any session: a refused case set leaves no run behind.
        from .epistemic.cases import load_split, require_contest_sets

        projects = load_split(RUN_ROOT, split, project)
        require_contest_sets(projects, arms)
    indexes = (
        [i for i in clbench_indexes(split, seeds) if i is not None] if suite == "clbench" else []
    )
    # Refuses a credits declaration without a key before the record or any session exists.
    billing.require_billing_env(os.environ)
    run_dir = runs_dir() / run_id
    write_record(
        run_dir,
        {
            "suite": suite,
            "split": split,
            "arms": arms,
            # The run indexes played: a report reads them as the schedule.
            "seeds": sorted(indexes) or None,
            "limit": limit,
            "model": model,
            "effort": effort,
            # The billing source every session of this run must report (``billing.py``).
            "billing": billing.declared(),
            "gate": verdict,
            **record.checkout_state(),
            "cli": record.cli_version(),
            **(
                {"case_check": {n: check for n, (_, check) in projects.items()}} if projects else {}
            ),
        },
    )
    if suite == "clbench":
        run_clbench(run_dir, split, arms, seeds, jobs, limit, model, effort)
    elif suite == "lab":
        run_lab_suite(run_dir, split, arms, jobs, limit, model, effort)
    elif suite == "epistemic":
        run_epistemic(run_dir, split, arms, jobs, window, history, projects, model, effort)
    else:
        run_planted(run_dir, split, arms, jobs, limit, model, effort)
    return run_dir


# --------------------------------------------------------------------------
# Reporting a suite.
# --------------------------------------------------------------------------


def present(pairs: tuple[str, ...], arms) -> list[tuple[str, str]]:
    """The suite's comparisons whose two arms both ran."""
    split = [tuple(p.split("-", 1)) for p in pairs]
    return [(a, b) for a, b in split if a in arms and b in arms]


def epistemic_report(
    run_dir: Path, stored: dict, spec, wanted, arms, jobs: int, earlier: list[dict] | None = None
) -> dict:
    """Judge an f38 run's answers, then build its report from every project of the split it played."""
    from .epistemic import cases, judge, report

    if spec is None:
        raise refuse("epistemic needs a judge: add --judge claude or --judge codex")
    projects = cases.load_split(RUN_ROOT, stored["split"])
    cases.require_unchanged_since_run(projects, stored.get("case_check", {}))
    judge_run = run_dir / f"judge-{spec.backend}"
    judge.items(run_dir, projects, judge_run, arms)
    judge.judge(judge_run, spec, REJUDGE_FRACTION, jobs)
    return {
        "suite": "epistemic",
        "run_id": run_dir.name,
        "judge_family": spec.family,
        **report.build(
            run_dir,
            projects,
            judge_run,
            wanted,
            arms,
            heldout=stored["split"] == "heldout",
            earlier=earlier or (),
        ),
    }


def report_suite(
    suite: str,
    run_ids: list[str],
    spec: JudgeSpec | None,
    *,
    jobs: int = 1,
    arms: list[str] | None = None,
    earlier: list[dict] | None = None,
) -> dict:
    """The unstamped report of runs of one suite. Lab and planted judge first; CL-Bench needs no judge.

    ``earlier`` holds the epistemic reports before this one, newest first, for the ``saturated`` reading."""
    run_dir = runs_dir() / run_ids[0]
    stored = read_record(run_dir)
    heldout = stored["split"] == "heldout"
    have = arms or stored["arms"] or list(suite_arms(suite))
    pairs = PAIRS[suite]
    if suite == "lab":
        # WG ablates the arm whose vault it copied: W in runs before WP existed, WP since.
        manifest = json.loads((run_dir / "manifest.json").read_text())
        pairs += (f"WG-{manifest.get('wg_copy_of', 'W')}",)
    wanted = present(pairs, have)
    if suite == "clbench":
        from .clbench import report

        built = report.build(
            run_dir, wanted, scheduled=stored.get("seeds"), heldout=heldout, arms=have
        )
    elif suite == "epistemic":
        built = epistemic_report(run_dir, stored, spec, wanted, have, jobs, earlier)
    else:
        if spec is None:
            raise refuse(f"{suite} needs a judge: add --judge claude or --judge codex")
        judge_run = run_dir / f"judge-{spec.backend}"
        if suite == "lab":
            from .lab import judge

            judge.items(judge_run, run_ids, list(suite_arms("lab")))
        else:
            from .planted import judge

            judge.items(planted_cases(stored["split"], stored["limit"]), run_dir, judge_run, have)
        judge.judge(judge_run, spec, REJUDGE_FRACTION, jobs)
        built = {
            "suite": suite,
            "run_id": run_ids[0],
            # The decision judge shares the actor's model family; every judged report says so.
            "judge_family": spec.family,
            **judge.score(judge_run, wanted, heldout=heldout),
        }
        if suite == "lab":
            built["runs"] = {
                "replayed": [
                    {"run": run_id, **entry}
                    for run_id in run_ids
                    for entry in replayed(runs_dir() / run_id, "matter")
                ]
            }
    run_dirs = [runs_dir() / r for r in run_ids]
    built["actor"] = record.run_identity(*run_dirs)
    built["billing"] = {"declared": stored.get("billing"), "observed": built["actor"]["billing"]}
    return built


def rejudge_sample(suite: str, run_id: str, primary: JudgeSpec, spec: JudgeSpec, jobs: int) -> dict:
    """Re-judge a seeded random share of the decision items with ``spec``, into a judging directory of its own.

    The agreement block it returns is description only: the decision reads the primary judge's
    verdicts, which this never touches. A judgement that failed is counted as unjudged, not as a disagreement.
    """
    if suite == "lab":
        from .lab import judge
    elif suite == "epistemic":
        from .epistemic import judge
    else:
        from .planted import judge
    run_dir = runs_dir() / run_id
    judge_run = run_dir / f"judge-{primary.backend}"
    sample_run = run_dir / f"judge-{spec.backend}-sample"
    sampled = write_sample(judge_run, sample_run, REJUDGE_FRACTION, judge.SEED)
    judge.judge(sample_run, spec, 0.0, jobs)
    return {
        "judge": spec.record(),
        "judge_family": spec.family,
        "description_only": "the decision reads the primary judge's verdicts only",
        "items_sampled": sampled,
        **judge.sample_agreement(judge_run, sample_run),
    }


def summary(report: dict) -> list[str]:
    """One line per paired measure and per arm's units, then the decision when the report has one."""
    # Billing leads: a run whose sessions were billed otherwise than declared changes how every figure reads.
    stated = report.get("billing") or {}
    lines = [billing.line(stated.get("declared"), stated.get("observed") or [])]
    if inferred := billing.judge_line(report.get("judge")):
        lines.append(inferred)
    if report.get("suite") == "epistemic":
        from .epistemic.report import summary_lines

        lines += summary_lines(report)
    for pair, body in report["pairs"].items():
        measures = {"": body} if "mean_difference" in body else body
        for name, m in measures.items():
            label = f"{pair} {name}".strip() + (f" ({m['role']})" if m.get("role") else "")
            if m.get("mean_difference") is None:
                lines.append(f"{label}: {m.get('verdict', 'unknown')}")
            else:
                lines.append(
                    f"{label}: {m['mean_difference']:+} (95% CI {m['ci95'][0]} to {m['ci95'][1]}) {m['verdict']}"
                )
    for arm, body in report["arms"].items():
        units = body["units"]
        lines.append(
            f"{arm} units: {units['scheduled']} scheduled, {units['valid']} valid, "
            f"{units['invalid']} invalid, {'unknown' if units['missing'] is None else units['missing']} missing"
            + (f", {body['unanswered']} unanswered" if "unanswered" in body else "")
        )
    sample = report.get("rejudge_sample")
    if sample:
        lines.append(
            f"rejudge sample by {sample['judge']['model']} (description only): "
            f"{sample['items_judged']} of {sample['items_sampled']} items judged, agreement {sample['agreement']}"
        )
    decision = report.get("decision")
    if decision:
        lines.append(f"decision: {decision['status']}")
        if "run_indexes_played" in decision:
            lines.append(f"  run indexes played: {decision['run_indexes_played']}")
        why = decision.get("why") or []
        lines += [f"  {reason}" for reason in ([why] if isinstance(why, str) else why)]
        lines += [
            f"{name}: {rule['result']}" for name, rule in decision.items() if isinstance(rule, dict)
        ]
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


def _csv(text: str | None) -> list[str] | None:
    return [part for part in text.split(",") if part] if text else None


def _cmd_fetch(args: argparse.Namespace) -> int:
    from .fetch import fetch

    fetch(RUN_ROOT)
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    with waits_for_room():
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
            window=args.window,
            project=args.project,
            history=args.history,
        )
    print(
        f"{args.suite} {args.split} run in {run_dir}; next: membench outcome report {args.suite} --run-id {args.run_id}"
    )
    return 0


def _earlier_reports(args: argparse.Namespace) -> list[dict]:
    """The stored reports that ``--earlier-report`` names, newest first as given; each must be an epistemic report."""
    stored = []
    for path in getattr(args, "earlier_report", None) or []:
        found = json.loads(Path(path).read_text())
        if found.get("suite") != "epistemic" or args.suite != "epistemic":
            raise refuse(f"--earlier-report {path} is not an epistemic report of an epistemic run")
        stored.append(found)
    return stored


def _cmd_report(args: argparse.Namespace) -> int:
    run_dir = runs_dir() / args.run_id
    if not run_dir.is_dir():
        raise refuse(f"no run at {run_dir}")
    stored = read_record(run_dir)
    if stored.get("suite", args.suite) != args.suite:
        raise refuse(f"{run_dir} is a {stored['suite']} run, not {args.suite}")
    if args.rejudge_sample and args.suite == "clbench":
        raise refuse("--rejudge-sample re-judges lab and planted items; CL-Bench needs no judge")
    spec = (
        JudgeSpec.of(args.judge, args.judge_model, args.judge_effort, args.judge_exe)
        if args.suite != "clbench"
        else None
    )
    if args.rejudge_sample and args.rejudge_sample == spec.backend:
        raise refuse(
            f"--rejudge-sample is a second judge, and {spec.backend} is this report's own: "
            f"name the other backend"
        )
    if spec and stored["split"] == "heldout" and (spec.backend, spec.model) != DECISION_JUDGE:
        raise refuse(
            f"a held-out {args.suite} report is judged by {DECISION_JUDGE[1]} on {DECISION_JUDGE[0]}: "
            f"add --judge {DECISION_JUDGE[0]}. Other judges serve development rounds only"
        )
    built = report_suite(
        args.suite, [args.run_id], spec, jobs=args.jobs, earlier=_earlier_reports(args)
    )
    built["run"] = {
        k: stored.get(k) for k in ("model", "effort", "billing", "arms", "seeds", "gate")
    }
    stamped = gate.stamp(built, stored)
    target = run_dir / (f"report-{spec.backend}.json" if spec else "report.json")
    target.write_text(json.dumps(stamped, indent=2))
    if args.rejudge_sample:
        # The primary report is already on disk: the optional sample cannot cost it, and never changes it.
        stamped["rejudge_sample"] = rejudge_sample(
            args.suite, args.run_id, spec, JudgeSpec.of(args.rejudge_sample), args.jobs
        )
        target.write_text(json.dumps(stamped, indent=2))
    print(f"{stamped['label']} ({stamped['evidence']} evidence, {stamped['split']} split)")
    print("\n".join(summary(stamped)))
    print(f"report: {target}")
    return 0


def _cmd_panel(args: argparse.Namespace) -> int:
    from .panel import run_panel

    spec = JudgeSpec.of(args.judge, args.judge_model, args.judge_effort, args.judge_exe)
    with waits_for_room():
        written = run_panel(
            args.run_id,
            args.version,
            spec,
            jobs=args.jobs,
            model=args.model,
            effort=args.effort,
            controls=args.controls,
        )
    print("\n".join(str(path) for path in written))
    return 0


def register(sub) -> None:
    """Add the ``outcome`` group to the ``membench`` command line."""
    group = sub.add_parser(
        "outcome", help="outcome evaluations on subscription or API-credit actors"
    )
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
    p_run.add_argument(
        "--arms",
        help="comma-separated arm codes; default all of the suite's arms, except epistemic's G",
    )
    p_run.add_argument(
        "--seeds",
        help="CL-Bench only: comma-separated run indexes. A held-out run plays 1; 1,2 and 1,2,3 add orderings in order",
    )
    p_run.add_argument("--jobs", type=int, default=1, help="parallel cells, tasks or arms")
    p_run.add_argument(
        "--limit", type=int, help="dev only: play the first N questions, tasks or cases"
    )
    p_run.add_argument(
        "--window",
        type=int,
        help="epistemic only: the window number to play into (default 1). A decision reads two windows "
        "of every project at least 6 hours apart",
    )
    p_run.add_argument(
        "--project",
        help="epistemic only: play this one project of the split, for example to replay an invalid window in a new --window",
    )
    p_run.add_argument(
        "--history",
        choices=("full",),
        help="epistemic dev only: the solvable gate. Arm N answers each probe with the whole history in its prompt",
    )
    _model_args(p_run)
    p_run.set_defaults(func=_cmd_run)

    p_report = commands.add_parser(
        "report", help="judge where the suite needs a judge and write the per-case report"
    )
    p_report.add_argument("suite", choices=SUITES)
    p_report.add_argument("--run-id", required=True)
    p_report.add_argument("--jobs", type=int, default=1, help="parallel judge calls")
    p_report.add_argument(
        "--earlier-report",
        action="append",
        metavar="PATH",
        help="an epistemic report that came before this one, newest first; give it once for each "
        "earlier report that a family's `saturated` reading needs",
    )
    p_report.add_argument(
        "--rejudge-sample",
        choices=sorted(DEFAULT_MODELS),
        help="also re-judge a seeded random 20%% of the items with this backend's judge, "
        "into files of their own, and add the agreement to the report; it never changes a decision",
    )
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
