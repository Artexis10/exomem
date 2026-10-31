"""``python -m membench.utility.organisation_replay {bind,run,check,score}``.

Run it once per arm under that arm's interpreter, with ``PYTHONPATH`` set to
the checkout's ``benchmarks`` directory and nothing else: the server, the tap,
the seed builder and the readback then all load the arm's own package.

1. ``bind`` writes ``OUT/binding-ARM.json`` from a slot of its own.
2. ``run`` runs each situation file through that binding.
3. ``check`` reports each way a run file departs from ``SCHEMA.md``.
4. ``score`` scores both arms' run files against the keys (``score.py``).

Exit codes are switch evaluation's: 0 done, 2 refused, aborted or stopped after
a struck situation, 75 paused on a quota. Rerun the same ``run`` command to
resume: it skips each finished or struck situation and resumes a paused or
aborted one. A situation that aborted on a fault the lane judges its own is
closed with ``run --strike ID --reason TEXT``.
"""

from __future__ import annotations

import argparse
import json
import shlex
import sys
from pathlib import Path

from membench.utility.switch_eval.arms import RETRIEVAL_PROFILES


def _config(args, binding: dict | None = None):
    from epistemic.journeys.f27_replay import AgentEnvelope, discover_agent_envelope

    from .runner import RunConfig
    from .standin import VERSION

    if binding is not None:
        client = binding["client"]
        return RunConfig(
            out=Path(args.out),
            arm=binding["arm"],
            profile=binding["profile"],
            model=client["model"],
            client=AgentEnvelope(Path(client["executable"]), client["version"]),
            launcher=tuple(client["launcher"]),
            revision=binding["revision"],
            base_revision=binding["base_revision"],
            wheel=None,
            standin=client["standin"],
        )
    if args.standin:
        # The dry run's actor: a scripted client behind the launcher seam, no model.
        envelope = AgentEnvelope(Path("claude-standin"), VERSION)
        # The client's environment carries no PYTHONPATH, so the launcher names the benchmarks root.
        benchmarks = Path(__file__).resolve().parents[3]
        launcher = (
            sys.executable,
            "-c",
            "import sys; sys.path.insert(0, sys.argv.pop(1)); "
            "from membench.utility.organisation_replay.standin import main; sys.exit(main())",
            str(benchmarks),
        )
    else:
        if args.claude:
            import subprocess

            version = subprocess.run(
                [args.claude, "--version"], capture_output=True, text=True, check=True
            )
            envelope = AgentEnvelope(Path(args.claude), version.stdout.strip())
        else:
            envelope = discover_agent_envelope()
        launcher = tuple(shlex.split(args.claude_launcher)) if args.claude_launcher else ()
    return RunConfig(
        out=Path(args.out),
        arm=args.arm,
        profile=args.profile,
        model=args.model,
        client=envelope,
        launcher=launcher,
        revision=args.revision,
        base_revision=args.base_revision,
        wheel=Path(args.wheel) if args.wheel else None,
        standin=args.standin,
        role=args.role,
    )


def cmd_bind(args) -> int:
    from .runner import bind

    record = bind(_config(args))
    print(
        f"bound {record['binding_ref']}: {len(record['shapes']['hookless']['tools'])} tools served"
    )
    return 0


def cmd_run(args) -> int:
    from membench.utility.switch_eval.__main__ import EXIT_PAUSED, EXIT_REFUSED
    from membench.utility.switch_eval.session import RunAborted, RunPaused

    from .runner import arm_parity, finished_run, run_situation, strike_situation
    from .situation import load_situation

    out = Path(args.out)
    binding = json.loads((out / f"binding-{args.arm}.json").read_text(encoding="utf-8"))
    parity = arm_parity(binding)
    for path in sorted(out.glob("binding-*.json")):
        other = arm_parity(json.loads(path.read_text(encoding="utf-8")))
        if differ := sorted(name for name in parity if other[name] != parity[name]):
            print(
                f"refused: {path.name} differs from this arm's binding in {', '.join(differ)}",
                file=sys.stderr,
            )
            return EXIT_REFUSED
    client = binding["client"]
    if not client["standin"]:
        import subprocess

        version = subprocess.run(
            [client["executable"], "--version"], capture_output=True, text=True, check=True
        ).stdout.strip()
        if version != client["version"]:
            print(
                f"refused: the client is {version}, the binding pinned {client['version']}",
                file=sys.stderr,
            )
            return EXIT_REFUSED
    manifest = Path(args.manifest) if args.manifest else None
    # Every file is checked before the first session starts.
    situations = [load_situation(Path(path), manifest=manifest) for path in args.situation]
    config = _config(args, binding)
    if args.strike is not None:
        chosen = [situation for situation in situations if situation.id == args.strike]
        if not chosen or not args.reason:
            print(
                "refused: --strike names one of the situation files and needs --reason",
                file=sys.stderr,
            )
            return EXIT_REFUSED
        path = strike_situation(config, binding, chosen[0], args.reason)
        print(f"{args.strike} {config.arm}: struck by the lane -> {path}")
        return 0
    for situation in situations:
        done = finished_run(config, binding, situation)
        if done is not None:
            print(f"{situation.id} {config.arm}: {done['status']}, finished earlier")
            continue
        try:
            path = run_situation(config, binding, situation)
        except RunPaused as pause:
            print(f"paused: {situation.id}: {pause}; rerun to resume", file=sys.stderr)
            return EXIT_PAUSED
        except RunAborted as abort:
            print(f"aborted: {situation.id}: {abort}; rerun to resume", file=sys.stderr)
            return EXIT_REFUSED
        run = json.loads(path.read_text(encoding="utf-8"))
        print(f"{situation.id} {config.arm}: {run['status']} -> {path}")
        if run["status"] == "struck":
            # More than two strikes void an f34 run, so one cause must not strike them all.
            print(
                f"stopped: {situation.id} is struck; read its faults, then rerun to continue",
                file=sys.stderr,
            )
            return EXIT_REFUSED
    return 0


def cmd_check(args) -> int:
    from .runfile import check_run

    run_dir = Path(args.run_dir)
    problems = check_run(json.loads((run_dir / "run.json").read_text(encoding="utf-8")), run_dir)
    for problem in problems:
        print(problem)
    print(
        f"{run_dir / 'run.json'}: {'conforms to SCHEMA.md' if not problems else f'{len(problems)} problems'}"
    )
    return 0 if not problems else 1


def cmd_score(args) -> int:
    from .score import ScoreRefused, score, write_report

    judgements = (
        json.loads(Path(args.judgements).read_text(encoding="utf-8")) if args.judgements else None
    )
    try:
        report = score(
            Path(args.out),
            Path(args.keys),
            baseline=args.baseline,
            candidate=args.candidate,
            manifest=Path(args.manifest) if args.manifest else None,
            judgements=judgements,
        )
    except ScoreRefused as refused:
        print(f"refused: {refused}", file=sys.stderr)
        return 2
    write_report(report, Path(args.to))
    stats = report["statistics"]
    print(
        f"{len(report['rows'])} scored, {len(report['struck'])} struck, "
        f"{len(report['incomplete'])} incomplete, {len(report['pending_judgement'])} awaiting the judge"
        + (f"; void: {len(report['struck'])} struck" if report["void"] else "")
    )
    print(stats if isinstance(stats, str) else json.dumps(stats["decision_rule"], sort_keys=True))
    return 0


def main(argv: list[str] | None = None) -> int:
    from .runner import REASON_CAPABLE

    parser = argparse.ArgumentParser(prog="organisation_replay")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("bind", help="write the arm's binding record")
    p.add_argument("--out", required=True, help="the run root, outside every repository")
    p.add_argument("--arm", required=True, help="the opaque arm label")
    p.add_argument("--revision", required=True, help="the arm's 40-hex Git revision")
    p.add_argument("--base-revision", required=True, help="the base revision both arms share")
    p.add_argument("--wheel", help="the wheel the arm's interpreter installed")
    p.add_argument("--model", required=True, help="the actor model id the sessions must report")
    p.add_argument("--profile", default="deployed", choices=sorted(RETRIEVAL_PROFILES))
    p.add_argument("--claude", help="the agent CLI executable (default: the first on PATH)")
    p.add_argument(
        "--claude-launcher", help="a command that execs the client as LAUNCHER CLIENT -- ARGS"
    )
    p.add_argument("--standin", action="store_true", help="dry run: a scripted client, no model")
    p.add_argument(
        "--role", required=True, choices=sorted(REASON_CAPABLE), help="the arm's comparison role"
    )
    p.set_defaults(func=cmd_bind)

    p = sub.add_parser("run", help="run situation files through a binding")
    p.add_argument("--out", required=True)
    p.add_argument("--arm", required=True)
    p.add_argument(
        "--situation", required=True, action="append", help="a situation file (repeatable)"
    )
    p.add_argument(
        "--manifest", help="the custody manifest (sha256sum format); required for held-out files"
    )
    p.add_argument(
        "--strike", metavar="ID", help="close this aborted situation as struck, and run nothing"
    )
    p.add_argument("--reason", help="why the lane judges the strike's fault the situation's own")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("check", help="check a run directory's run.json against SCHEMA.md")
    p.add_argument("--run-dir", required=True)
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("score", help="score both arms' run files against the key files")
    p.add_argument("--out", required=True, help="the run root that holds OUT/<situation>/<arm>/")
    p.add_argument("--keys", required=True, help="the directory that holds the key files")
    p.add_argument("--baseline", required=True, help="the baseline's opaque arm label")
    p.add_argument("--candidate", required=True, help="the candidate's opaque arm label")
    p.add_argument("--manifest", help="the custody manifest; required for held-out keys")
    p.add_argument("--judgements", help="the blind judge's verdicts on the residual items")
    p.add_argument("--to", required=True, help="where scores.json and the judge files go")
    p.set_defaults(func=cmd_score)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
