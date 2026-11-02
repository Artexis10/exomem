"""`python -m benchmarks.import_guidance run ...` from the repository root."""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

# The reused harnesses (`epistemic`, `membench`) are top-level packages under benchmarks/.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from import_guidance import report, runner, session_close, session_score, store_report  # noqa: E402
from import_guidance.recording import parse_arm  # noqa: E402
from import_guidance.scripted_replies import DEFAULT_SUITE  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="import_guidance")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="run scenarios through a real headless agent")
    run.add_argument("--model", required=True)
    run.add_argument(
        "--judge-model", default="opus", help="the judge's model; not the agent's by default"
    )
    run.add_argument("--repeats", type=int, default=1)
    run.add_argument(
        "--repeat-offset",
        type=int,
        default=0,
        help="number the repeats from here, to add runs to an existing --out folder",
    )
    run.add_argument(
        "--scenario", default="all", help="a scenario id, an id prefix such as 03, or all"
    )
    run.add_argument(
        "--suite",
        default=DEFAULT_SUITE,
        help="a scenario folder: `import` (scenarios/) or a name under scenarios/, e.g. write-narration. "
        "`session-close` runs real sessions in a seeded vault; it needs --product-root arms and no judge here",
    )
    run.add_argument(
        "--product-root",
        action="append",
        default=[],
        metavar="[LABEL=]PATH",
        help="an arm: a checkout whose own tool text, skill, terminal and refusals the agent is shown. "
        "Repeat for a comparison; with none the current checkout is shown, as before",
    )
    run.add_argument(
        "--out", type=Path, required=True, help="outside every repository, e.g. under /tmp"
    )
    run.add_argument(
        "--dry-run", action="store_true", help="check files, tools and logging; start no model"
    )
    rep = sub.add_parser(
        "report", help="leak and pass rates per case, model and arm from runs.jsonl"
    )
    rep.add_argument("runs", type=Path, nargs="+", help="one or more runs.jsonl files")
    rep.add_argument("--baseline", required=True, help="the baseline arm's label")
    rep.add_argument("--candidate", required=True, help="the candidate arm's label")
    rep.add_argument("--out", type=Path, help="write the report here instead of printing it")
    store = sub.add_parser(
        "store-report", help="pass counts for the store-identity cases (07 to 09) from runs.jsonl"
    )
    store.add_argument("runs", type=Path, nargs="+", help="one or more runs.jsonl files")
    store.add_argument("--baseline", required=True, help="the baseline arm's label")
    store.add_argument("--candidate", required=True, help="the candidate arm's label")
    store.add_argument("--out", type=Path, help="write the tables here instead of printing them")
    score = sub.add_parser(
        "session-score",
        help="score session-close runs against the answer key and a blind judge; refuses while a key is missing",
    )
    score.add_argument("runs", type=Path, nargs="+", help="one or more runs.jsonl files")
    score.add_argument("--baseline", required=True, help="the baseline arm's label")
    score.add_argument("--candidate", required=True, help="the candidate arm's label")
    score.add_argument("--judge-model", default="opus")
    score.add_argument("--out", type=Path, required=True, help="a folder for scores.jsonl and report.md")
    args = parser.parse_args(argv)

    if args.command == "session-score":
        scored = session_score.score_all(args.runs, out=args.out, judge_model=args.judge_model)
        text = session_score.build_report(scored, baseline=args.baseline, candidate=args.candidate)
        (args.out / "report.md").write_text(text, encoding="utf-8")
        print(text)
        return 0
    if args.command == "store-report":
        text = store_report.build(args.runs, baseline=args.baseline, candidate=args.candidate)
        if args.out:
            args.out.write_text(text, encoding="utf-8")
        else:
            print(text)
        return 0
    if args.command == "report":
        text = report.build(args.runs, baseline=args.baseline, candidate=args.candidate)
        if args.out:
            args.out.write_text(text, encoding="utf-8")
        else:
            print(text)
        return 0

    arms = [parse_arm(text) for text in args.product_root]
    if args.suite == session_close.SUITE:
        return run_session_close(args, arms)
    scenarios = runner.load_scenarios(args.scenario, args.suite)
    if args.dry_run:
        failed = 0
        with tempfile.TemporaryDirectory(prefix="ig-dry-") as scratch:
            for scenario in scenarios:
                for arm in arms or [None]:
                    problems = runner.dry_run(scenario, Path(scratch), arm)
                    label = f"{arm.label}: " if arm else ""
                    print(
                        f"{label}{scenario.id}: "
                        f"{'ok' if not problems else 'PROBLEMS ' + '; '.join(problems)}"
                    )
                    if not problems:
                        argv_preview = runner.agent_argv(
                            Path("claude"),
                            scenario,
                            mcp=Path("mcp.json"),
                            skill=Path("skill.md"),
                            model=args.model,
                        )
                        print(
                            "  would run:",
                            " ".join(argv_preview[:2] + [repr(argv_preview[2])] + argv_preview[3:]),
                        )
                    failed += bool(problems)
        return 1 if failed else 0
    rows = runner.run_all(
        scenarios,
        model=args.model,
        judge_model=args.judge_model,
        repeats=args.repeats,
        out=args.out,
        arms=arms,
        repeat_offset=args.repeat_offset,
    )
    print(runner.summary_table(rows))
    return 0


def run_session_close(args: argparse.Namespace, arms: list) -> int:
    cases = session_close.load_cases(args.scenario)
    if not arms:
        raise SystemExit("the session-close suite needs at least one --product-root arm")
    if args.dry_run:
        failed = 0
        with tempfile.TemporaryDirectory(prefix="ig-dry-") as scratch:
            for case in cases:
                for arm in arms:
                    problems = session_close.dry_run(case, arm, Path(scratch))
                    print(f"{arm.label}: {case.id}: {'ok' if not problems else 'PROBLEMS ' + '; '.join(problems)}")
                    failed += bool(problems)
        return 1 if failed else 0
    rows = session_close.run_all(
        cases,
        model=args.model,
        repeats=args.repeats,
        out=args.out,
        arms=arms,
        repeat_offset=args.repeat_offset,
    )
    return 1 if any(row["error"] for row in rows) else 0


if __name__ == "__main__":
    raise SystemExit(main())
