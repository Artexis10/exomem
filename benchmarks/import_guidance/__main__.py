"""`python -m benchmarks.import_guidance run ...` from the repository root."""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

# The reused harnesses (`epistemic`, `membench`) are top-level packages under benchmarks/.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from import_guidance import (  # noqa: E402
    judge_batch,
    replay_cases,
    replay_report,
    report,
    runner,
    session_close,
    session_score,
    session_table,
    store_report,
)
from import_guidance.recording import (  # noqa: E402
    graph_states,
    load_recording,
    parse_arm,
    served_sizes,
)
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
    run.add_argument("--case-dir", type=Path, help="session-close case inputs; a seed in <case-dir>/seeds wins over a shipped seed of the same name")
    run.add_argument("--client-mode", choices=session_close.CLIENT_MODES, help="session-close client configuration; default plugin installs hooks and skill, tool-only requests disabled hooks and omits skill")
    run.add_argument(
        "--ready-graph",
        action="store_true",
        help="session-close only: build each seeded vault's relation graph before the session, as a live vault has it "
        "(the arm's own reconcile, then wait until its recall_context reports the graph available)",
    )
    run.add_argument("--credits-account", help="session-close only: select the maintained credits launcher account")
    run.add_argument("--credits-budget-usd", type=float, help="maximum requested for each capture or fresh process; requires account and reason")
    run.add_argument("--credits-reason", help="the maintained credits launcher's reason; requires account and budget")
    run.add_argument(
        "--suite",
        default=DEFAULT_SUITE,
        help="a scenario folder: `import` (scenarios/), a name under scenarios/ such as write-narration, or the path "
        "of a folder of scenario files (`write-scenarios` makes one). "
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
    run.add_argument(
        "--hook-turn",
        action="store_true",
        help="session-close only: after the close turn, send the arm's own episode ask (the Stop hook's text) as one more turn",
    )
    write = sub.add_parser(
        "write-scenarios",
        help="write one context-replay scenario per case of a case folder; starts no model",
    )
    write.add_argument("--case-dir", type=Path, required=True, help="case files, with answer-keys/<id>.marks.json and seeds/")
    write.add_argument("--out", type=Path, required=True, help="outside every repository; gets scenarios/ and case-sets.json")
    prepare = sub.add_parser(
        "judge-prepare",
        help="export the blind judge items for stage-B answers and stage-C replies, shuffled together; starts no model",
    )
    prepare.add_argument("--stage-b", type=Path, nargs="*", default=[], help="runs.jsonl files of the replay suite")
    prepare.add_argument("--stage-c", type=Path, nargs="*", default=[], help="runs.jsonl files of the session-close suite")
    prepare.add_argument("--suite", required=True, help="the replay scenario folder that the runs used; it holds each case's turn, reference and criteria")
    prepare.add_argument("--out", type=Path, required=True, help="gets judge-views.jsonl, and private/judge-map.json for the owner of the key only")
    imported = sub.add_parser(
        "judge-import", help="read the native judgments of a batch by opaque id and report the judge agreement; starts no model"
    )
    imported.add_argument("--judgments", type=Path, required=True, help="JSONL lines of {id, structured_output}")
    imported.add_argument("--stage-b", type=Path, nargs="*", default=[], help="the stage-B runs.jsonl files given to judge-prepare")
    imported.add_argument("--stage-c", type=Path, nargs="*", default=[], help="the stage-C runs.jsonl files given to judge-prepare")
    imported.add_argument(
        "--suite", required=True, help="the scenario folder given to judge-prepare; a run whose prompt changed since then is unknown"
    )
    imported.add_argument("--out", type=Path, required=True, help="the folder judge-prepare wrote; gets judged.json")
    replay = sub.add_parser(
        "replay-report", help="per set of cases, the paired pass difference and reply-length ratio of two arms on stage-B answers"
    )
    replay.add_argument("runs", type=Path, nargs="+", help="one or more runs.jsonl files of the replay suite")
    replay.add_argument("--judged", type=Path, help="judged.json from judge-import; without it every judged criterion is unknown")
    replay.add_argument("--case-sets", type=Path, required=True, help="case-sets.json from write-scenarios")
    replay.add_argument("--criteria", nargs="+", help="the judged criteria that must hold; default is every criterion of each judgment, the case folder's rubric")
    replay.add_argument("--baseline", required=True, help="the baseline arm's label")
    replay.add_argument("--candidate", required=True, help="the candidate arm's label")
    replay.add_argument("--out", type=Path, help="write the report here instead of printing it")
    table = sub.add_parser("session-table", help="per pair, what each full session did, from session-close runs.jsonl")
    table.add_argument("runs", type=Path, nargs="+", help="one or more runs.jsonl files of the session-close suite")
    table.add_argument("--judged", type=Path, help="judged.json from judge-import, for the reply verdict column")
    table.add_argument("--baseline", required=True, help="the baseline arm's label")
    table.add_argument("--candidate", required=True, help="the candidate arm's label")
    table.add_argument("--out", type=Path, help="write the table here instead of printing it")
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
    score.add_argument("--case-dir", type=Path, help="case inputs with adjacent scorer-only answer-keys")
    score.add_argument("--baseline", required=True, help="the baseline arm's label")
    score.add_argument("--candidate", required=True, help="the candidate arm's label")
    score.add_argument("--judge-model", default="opus", help="used only with --automatic-judge")
    judging = score.add_mutually_exclusive_group()
    judging.add_argument("--prepare-judge", action="store_true", help="export shuffled blind views for a native evaluator; the default")
    judging.add_argument("--judgments", type=Path, help="import native JSONL judgments by opaque ID; starts no model")
    judging.add_argument("--automatic-judge", action="store_true", help="explicitly run the legacy Claude CLI judge")
    score.add_argument("--out", type=Path, required=True, help="a folder for scores.jsonl and report.md")
    args = parser.parse_args(argv)

    if args.command == "write-scenarios":
        paths = replay_cases.write_scenarios(args.case_dir, args.out)
        print(f"Wrote {len(paths)} scenarios to {args.out / 'scenarios'} and the case sets to {args.out / 'case-sets.json'}.")
        return 0
    if args.command == "judge-prepare":
        count = judge_batch.prepare(args.stage_b, args.stage_c, runner.load_scenarios("all", args.suite), out=args.out)
        print(f"Prepared {count} blind items in {args.out / 'judge-views.jsonl'}. Keep private/judge-map.json from the judge.")
        return 0
    if args.command == "judge-import":
        scenarios = runner.load_scenarios("all", args.suite)
        summary = judge_batch.import_judgments(args.judgments, out=args.out, stage_b=args.stage_b, stage_c=args.stage_c, scenarios=scenarios)
        print(judge_batch.agreement_line(summary))
        return 0
    if args.command in ("replay-report", "session-table"):
        if args.command == "replay-report":
            text = replay_report.build(args.runs, judged=args.judged, case_sets=args.case_sets, baseline=args.baseline, candidate=args.candidate, criteria=args.criteria)
        else:
            text = session_table.build(args.runs, baseline=args.baseline, candidate=args.candidate, judged=args.judged)
        if args.out:
            args.out.write_text(text, encoding="utf-8")
        else:
            print(text)
        return 0
    if args.command == "session-score":
        if args.prepare_judge or not (args.judgments or args.automatic_judge):
            count = session_score.prepare_native(args.runs, out=args.out, case_dir=args.case_dir)
            print(f"Prepared {count} blind items in {args.out / 'judge-views.jsonl'}. Keep private/judge-map.json from the evaluator.")
            return 0
        scored = session_score.score_all(args.runs, out=args.out, judge_model=args.judge_model, judgments=args.judgments, automatic_judge=args.automatic_judge, case_dir=args.case_dir)
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

    args.credits = None
    credits_options = (args.credits_account, args.credits_budget_usd, args.credits_reason)
    if any(value is not None for value in credits_options):
        if args.suite != session_close.SUITE:
            raise SystemExit("--credits-account, --credits-budget-usd and --credits-reason apply to the session-close suite only")
        if any(value is None for value in credits_options):
            raise SystemExit("--credits-account, --credits-budget-usd and --credits-reason are required together")
        try:
            args.credits = session_close.CreditsRun(*credits_options)
        except ValueError as error:
            raise SystemExit(str(error)) from error
    arms = [parse_arm(text) for text in args.product_root]
    if args.client_mode is not None and args.suite != session_close.SUITE:
        raise SystemExit("--client-mode applies to the session-close suite only")
    args.client_mode = args.client_mode or session_close.DEFAULT_CLIENT_MODE
    if args.case_dir is not None and args.suite != session_close.SUITE:
        raise SystemExit("--case-dir applies to the session-close suite only")
    if args.ready_graph and args.suite != session_close.SUITE:
        raise SystemExit("--ready-graph applies to the session-close suite only; a replay scenario carries its own")
    if args.hook_turn and args.client_mode == "tool-only":
        raise SystemExit("--hook-turn requires --client-mode plugin")
    if args.hook_turn and args.suite != session_close.SUITE:
        raise SystemExit("--hook-turn applies to the session-close suite only")
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
                    if not problems and arm and scenario.record:
                        sizes = served_sizes(Path(scratch) / f"{scenario.id}.{arm.label}.shapes.json")
                        shapes = Path(scratch) / f"{scenario.id}.{arm.label}.shapes.json"
                        built = load_recording(shapes).get("ready")
                        print(
                            "  recorded:",
                            ", ".join(f"{name} {size} chars" for name, size in sizes.items()),
                            *(f"| graph {name}: {state}" for name, state in graph_states(shapes).items()),
                            *([f"| graph built in {built['seconds']}s"] if built else []),
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
    cases = session_close.load_cases(args.scenario, case_dir=args.case_dir)
    if not arms:
        raise SystemExit("the session-close suite needs at least one --product-root arm")
    if args.dry_run:
        failed = 0
        with tempfile.TemporaryDirectory(prefix="ig-dry-") as scratch:
            for case in cases:
                for arm in arms:
                    problems = session_close.dry_run(
                        case,
                        arm,
                        Path(scratch),
                        hooked=args.hook_turn,
                        client_mode=args.client_mode,
                        ready_graph=args.ready_graph,
                    )
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
        hooked=args.hook_turn,
        client_mode=args.client_mode,
        credits=args.credits,
        ready_graph=args.ready_graph,
    )
    return 1 if any(row["error"] or (args.client_mode == "tool-only" and (row.get("fresh") or {}).get("error")) for row in rows) else 0


if __name__ == "__main__":
    raise SystemExit(main())
