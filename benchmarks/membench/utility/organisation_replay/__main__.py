"""``python -m membench.utility.organisation_replay {bind,run,check}``.

Run it once per arm under that arm's interpreter, with ``PYTHONPATH`` set to
the checkout's ``benchmarks`` directory and nothing else: the server, the tap,
the seed builder and the readback then all load the arm's own package.

1. ``bind`` writes ``OUT/binding-ARM.json`` from a slot of its own.
2. ``run`` runs each situation file through that binding.
3. ``check`` reports each way a run file departs from ``SCHEMA.md``.

Exit codes are switch evaluation's: 0 done, 2 refused or aborted, 75 paused on
a quota. Rerun the same ``run`` command to resume a paused or aborted situation.
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


def arm_parity(binding: dict) -> dict:
    """What must be equal across the arms, or the comparison measures it too."""

    client = binding["client"]
    return {
        "base_revision": binding["base_revision"],
        "model": client["model"],
        "client_version": client["version"],
        "standin": client["standin"],
        # A stand-in's launcher names its own arm's interpreter and checkout.
        "launcher": None if client["standin"] else client["launcher"],
        "profile": binding["profile"],
        "hook_scripts_sha256": {
            shape: facts.get("hook_scripts_sha256") for shape, facts in binding["shapes"].items()
        },
    }


def cmd_run(args) -> int:
    from membench.utility.switch_eval.__main__ import EXIT_PAUSED, EXIT_REFUSED
    from membench.utility.switch_eval.session import RunAborted, RunPaused

    from .runner import run_situation
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
    for situation in situations:
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
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("check", help="check a run directory's run.json against SCHEMA.md")
    p.add_argument("--run-dir", required=True)
    p.set_defaults(func=cmd_check)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
