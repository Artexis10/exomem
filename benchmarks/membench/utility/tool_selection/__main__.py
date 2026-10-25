"""The tool-selection evaluation's stages, one subcommand each.

Run from the repository root with the benchmarks on the path::

    PYTHONPATH=benchmarks uv run python -m membench.utility.tool_selection <stage> ...

1. ``plan`` prints each round's session count and token estimate; it starts nothing.
2. ``run`` seeds the cases, probes each shape's surface and plays every session;
   ``run --dry-run`` prints the plan and starts nothing.
3. ``readout`` scores the sessions and compares each arm with ``grouped``.
4. ``calibrate`` turns a finished run into the numbers ``plan`` estimates from.
5. ``generate`` draws a held-out case file. Run it once, after the dev round.

Exit codes: 0 done, 2 refused or aborted, 3 run void, 75 paused on a quota.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import asdict
from pathlib import Path

from membench.utility.switch_eval import protocol

EXIT_REFUSED = 2
EXIT_VOID = 3
EXIT_PAUSED = 75

#: The diagnostic round keeps two repeats. Root freezes a fresh decision size after development.
DEFAULT_REPEATS = 2
#: The committed calibration of ``plan``: the means of the live smoke sessions,
#: replaced by a dev round's own means with the ``calibrate`` stage.
DEFAULT_CALIBRATION = Path(__file__).resolve().parent / "calibration.json"


def _harness_digest() -> str:
    """Digest of this package's code, texts and pools, recorded with the run's inputs."""

    root = Path(__file__).resolve().parent
    files = [*root.glob("*.py"), *root.glob("*.json"), *(root / "texts").glob("*.md")]
    digest = hashlib.sha256()
    for path in sorted(files):
        if path.name in ("dev_cases.json", "calibration.json"):
            continue
        digest.update(path.name.encode() + b"\0" + path.read_bytes() + b"\0")
    return digest.hexdigest()


def _refuse_api_key(env) -> None:
    from membench.utility.switch_eval.session import API_KEY_VARIABLES

    present = [name for name in API_KEY_VARIABLES if env.get(name)]
    if present:
        raise SystemExit(
            f"refused: {', '.join(present)} is set; the actor runs on the subscription "
            "with no API key in the environment"
        )


def _arms(text: str) -> tuple[str, ...]:
    from .executor import SHAPES

    arms = tuple(text.split(","))
    if len(set(arms)) != len(arms) or [a for a in arms if a not in SHAPES]:
        raise SystemExit(f"--arms takes distinct arms from {', '.join(SHAPES)}, got {text}")
    return arms


def _cases(args: argparse.Namespace):
    from . import cases

    chosen = cases.load_cases(Path(args.cases)) if args.cases else cases.dev_cases()
    if args.case_ids:
        wanted = args.case_ids.split(",")
        chosen = [case for case in chosen if case.case_id in wanted]
        if len(chosen) != len(set(wanted)):
            raise SystemExit("--case-ids names a case the case file does not hold")
    return chosen


# --------------------------------------------------------------------------
# plan
# --------------------------------------------------------------------------


def cmd_plan(args: argparse.Namespace) -> int:
    from . import plan as planning

    arms = _arms(args.arms)
    path = Path(args.calibration) if args.calibration else DEFAULT_CALIBRATION
    calibration = planning.load_calibration(path) if path.exists() else None
    schema_bytes = dict(_pairs(args.schema_bytes, "--schema-bytes", int))
    sizes = {"dev": len(_cases(args))}
    for size in args.heldout_count or ():
        sizes[f"heldout-{size}"] = size
    rounds = {
        name: planning.estimate_round(
            cases=count,
            arms=arms,
            repeats=args.repeats,
            calibration=calibration,
            schema_bytes=schema_bytes,
        )
        for name, count in sizes.items()
    }
    for line in planning.format_plan(rounds, calibration):
        print(line)
    return 0


def _pairs(values: Sequence[str], what: str, kind=str) -> list[tuple[str, object]]:
    pairs = []
    for value in values or ():
        name, sep, rest = value.partition("=")
        if not sep or not name or not rest:
            raise SystemExit(f"{what} takes NAME=VALUE, got {value!r}")
        pairs.append((name, kind(rest)))
    return pairs


# --------------------------------------------------------------------------
# run
# --------------------------------------------------------------------------


def _envelope(args: argparse.Namespace):
    from epistemic.journeys.f27_replay import AgentEnvelope, discover_agent_envelope

    if args.claude:
        version = subprocess.run(
            [args.claude, "--version"], capture_output=True, text=True, check=True
        ).stdout.strip()
        envelope = AgentEnvelope(executable=Path(args.claude), version=version)
    else:
        envelope = discover_agent_envelope()
    # The native binary itself, not a link an update moves: the run pins it.
    return AgentEnvelope(executable=envelope.executable.resolve(), version=envelope.version)


def _record_inputs(layout, inputs: dict) -> None:
    path = layout.run / "inputs.json"
    if path.exists() and json.loads(path.read_text(encoding="utf-8")) != inputs:
        raise SystemExit(f"{path} holds other inputs; a run keeps its inputs (use a new run directory)")
    path.write_text(json.dumps(inputs, indent=2, sort_keys=True), encoding="utf-8")


def _probe_or_load(layout, shape: str, *, vault: Path, parent: dict, tool_search: str, probe: bool):
    from .executor import probe_arm

    path = layout.arm_dir(shape) / "probe.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    if not probe:
        return None
    record = probe_arm(layout, shape, vault=vault, parent_env=parent, tool_search=tool_search)
    path.write_text(json.dumps(record, indent=2, sort_keys=True), encoding="utf-8")
    return record


def cmd_run(args: argparse.Namespace) -> int:
    from membench.utility.switch_eval.session import (
        HARNESS_FAULT,
        STRUCK,
        PlannedSession,
        RunAborted,
        RunPaused,
        dry_run_lines,
        load_session,
        plan_sessions,
        run_session,
        write_arm_files,
    )
    from membench.utility.switch_eval.state import Layout, StateError, require_default_kb_dirname

    from . import executor as ex

    parent = dict(os.environ)
    _refuse_api_key(parent)
    try:
        require_default_kb_dirname()
    except StateError as error:
        raise SystemExit(f"refused: {error}") from error
    arms = _arms(args.arms)
    cases = _cases(args)
    by_id = {case.case_id: case for case in cases}
    layout = Layout.at(Path(args.run))
    envelope = _envelope(args)
    launcher = shlex.split(args.claude_launcher or "")
    case_payload = [case.as_dict() for case in cases]
    recorded = (
        json.loads(
            json.dumps(
                {
                    "design": "openspec/changes/complete-single-purpose-tools/design.md",
                    "harness_digest": _harness_digest(),
                    "actor_model": args.actor_model,
                    "client": {
                        "executable": str(envelope.executable),
                        "version": envelope.version,
                        "launcher": launcher,
                    },
                    "arms": list(arms),
                    "repeats": args.repeats,
                    "seed": args.seed,
                    "tool_search": args.tool_search,
                    "call_budget": args.call_budget,
                    "case_ids": [case.case_id for case in cases],
                    "cases": case_payload,
                    "cases_sha256": hashlib.sha256(json.dumps(case_payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
                }
            )
        )
    )
    if not args.dry_run:
        layout.run.mkdir(parents=True, exist_ok=True)
        _record_inputs(layout, recorded)
        (layout.run / "cases.json").write_text(
            json.dumps(case_payload, indent=2), encoding="utf-8"
        )
    plan = plan_sessions(cases, arm_ids=arms, repeats=args.repeats, seed=args.seed)
    first_spec = ex.arm_spec(arms[0], ("*",))

    probes = {}
    try:
        if not args.dry_run:
            seeds = {
                case.case_id: ex.seed_case(layout.run, case, parent_env=parent, spec=first_spec)
                for case in cases
            }
        else:
            seeds = {case.case_id: {"digest": "pending: not seeded in a dry run"} for case in cases}
        for shape in arms:
            probes[shape] = _probe_or_load(
                layout,
                shape,
                vault=layout.run / "seeded" / cases[0].case_id / "vault",
                parent=parent,
                tool_search=args.tool_search,
                probe=not args.dry_run,
            )
        if not args.dry_run:
            ex.require_distinct_shapes(probes)
    except StateError as error:
        print(f"refused: {error}", file=sys.stderr)
        return EXIT_REFUSED

    specs = {
        shape: ex.arm_spec(shape, probes[shape]["tools"] if probes[shape] else ("*",))
        for shape in arms
    }

    def inputs_for(planned: PlannedSession):
        return ex.session_inputs(
            layout=layout,
            planned=planned,
            case=by_id[planned.case_id],
            spec=specs[planned.arm],
            envelope=envelope,
            model=args.actor_model,
            parent_env=parent,
            seed_digest=seeds[planned.case_id]["digest"],
            tool_search=args.tool_search,
            budget=args.call_budget,
        )

    if args.dry_run:
        print(f"dry run of {layout.run}: {len(plan)} sessions, nothing is started")
        print(f"client {envelope.executable} {envelope.version}; actor model {args.actor_model}")
        print(f"tool search {args.tool_search}; call budget {args.call_budget}")
        for planned in plan:
            for line in dry_run_lines(inputs_for(planned), parent_env=parent):
                print(line)
        return 0

    version = subprocess.run(
        [str(envelope.executable), "--version"], capture_output=True, text=True, check=True
    ).stdout.strip()
    if version != envelope.version:
        print(f"refused: the client is {version}, the run pinned {envelope.version}", file=sys.stderr)
        return EXIT_REFUSED
    for spec in specs.values():
        write_arm_files(ex.arm_files(layout, spec, budget=args.call_budget))
    surfaces = {(shape, ex.SURFACE_TREE): probes[shape]["hashes"] for shape in arms}
    executor = ex.ToolSelectionExecutor(
        layout=layout,
        specs=specs,
        model=args.actor_model,
        client_version=version.split()[0],
        surfaces=surfaces,
        launcher=tuple(launcher),
        cases=by_id,
        tool_search=args.tool_search,
    )
    struck: set[str] = set()
    ran = 0
    for planned in plan:
        directory = layout.session_dir(planned.case_id, planned.arm, planned.repeat)
        record = load_session(directory)
        if record is not None:
            if record["outcome"] in (HARNESS_FAULT, STRUCK):
                struck.add(record["case_id"])
            continue
        if planned.case_id in struck:
            directory.mkdir(parents=True, exist_ok=True)
            (directory / "session.json").write_text(
                json.dumps(
                    {
                        **asdict(planned),
                        "outcome": STRUCK,
                        "kind": "case struck after harness faults",
                        "attempts": 0,
                        "turns": [],
                        "answer": None,
                        "checks": {},
                        "facts": {},
                    },
                    indent=2,
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
            continue
        if len(struck) > protocol.MAX_STRUCK_CASES:
            print(f"run void: {len(struck)} cases struck ({', '.join(sorted(struck))})", file=sys.stderr)
            return EXIT_VOID
        if args.max_sessions is not None and ran >= args.max_sessions:
            break
        try:
            record = run_session(inputs_for(planned), directory, executor)
        except RunPaused as pause:
            print(f"paused: {pause}; rerun this stage to resume", file=sys.stderr)
            return EXIT_PAUSED
        except RunAborted as abort:
            print(f"aborted: {abort}", file=sys.stderr)
            return EXIT_REFUSED
        ran += 1
        if record["outcome"] == HARNESS_FAULT:
            struck.add(record["case_id"])
        print(f"{planned.session_id}: {record['outcome']} {record['kind']} ({record['attempts']} attempts)")
    if len(struck) > protocol.MAX_STRUCK_CASES:
        print(f"run void: {len(struck)} cases struck", file=sys.stderr)
        return EXIT_VOID
    return 0


# --------------------------------------------------------------------------
# readout, calibrate, generate, seed-vault
# --------------------------------------------------------------------------


def _scored_run(args: argparse.Namespace):
    from membench.utility.switch_eval.state import Layout

    from . import cases as case_module
    from .readout import load_results
    from .resolve import RESOLVERS

    layout = Layout.at(Path(args.run))
    cases = {
        case.case_id: case
        for case in case_module.load_cases(layout.run / "cases.json")
    }
    directories = sorted(layout.sessions.glob("session-*")) if layout.sessions.exists() else []
    return cases, load_results(directories, cases, RESOLVERS[args.resolver]())


def cmd_readout(args: argparse.Namespace) -> int:
    from . import readout as ro

    cases, results = _scored_run(args)
    inputs_path = Path(args.run) / "inputs.json"
    inputs = json.loads(inputs_path.read_text()) if inputs_path.exists() else {}
    report = ro.readout(results, cases, delta=args.delta, seed=args.seed,
                        expected_repeats=inputs.get("repeats"), expected_arms=inputs.get("arms"))
    ro.write_readout(report, Path(args.run) / "readout.json")
    print(ro.format_readout(report))
    return 0


def cmd_calibrate(args: argparse.Namespace) -> int:
    from . import plan as planning

    _cases_by_id, results = _scored_run(args)
    calibration = planning.calibrate(results, source=args.source or f"run {Path(args.run).name}")
    Path(args.out).write_text(json.dumps(calibration.as_dict(), indent=2, sort_keys=True), encoding="utf-8")
    print(f"wrote {args.out}: {calibration.as_dict()}")
    return 0


def cmd_generate(args: argparse.Namespace) -> int:
    from . import cases as case_module

    out = Path(args.out)
    if out.exists():
        raise SystemExit(f"refused: {out} exists; a held-out set is generated once and then frozen")
    chosen = case_module.generate_heldout(args.count, args.master_seed)
    case_module.write_cases(chosen, out)
    print(f"wrote {len(chosen)} held-out cases to {out} (master seed {args.master_seed})")
    return 0


def cmd_seed_vault(args: argparse.Namespace) -> int:
    from .cases import Case
    from .vault import read_end_state, seed_vault

    raw = json.loads(args.case_json)
    case = Case(raw["case_id"], raw["template"], raw["seed"], raw["params"])
    seed_vault(Path(args.out), case.seed_steps())
    if args.snapshot_out:
        snapshot = read_end_state(Path(args.out))
        Path(args.snapshot_out).write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
        if case.template_id == "source-derived":
            from .development import admit_fixture

            admit_fixture(case.params, snapshot)
    return 0


def cmd_snapshot_vault(args: argparse.Namespace) -> int:
    from .vault import read_end_state

    Path(args.out).write_text(json.dumps(read_end_state(Path(args.vault)), indent=2), encoding="utf-8")
    return 0


def cmd_admit(args: argparse.Namespace) -> int:
    """Materialize source fixtures without a host adapter or an actor."""
    from membench.utility.switch_eval.state import Layout, StateError

    from .executor import arm_spec, seed_case

    layout = Layout.at(Path(args.run))
    layout.run.mkdir(parents=True, exist_ok=True)
    rows = []
    for case in _cases(args):
        if case.params.get("blocked"):
            row = {"case_id": case.case_id, "status": "blocked", "reason": case.params["blocked"]}
        else:
            try:
                row = {"case_id": case.case_id, "status": "admitted",
                       **seed_case(layout.run, case, parent_env=dict(os.environ), spec=arm_spec("grouped", ("*",)))}
            except StateError as error:
                row = {"case_id": case.case_id, "status": "failed", "reason": str(error)}
        rows.append(row)
        print(f"{case.case_id}: {row['status']}")
    record = {"harness_digest": _harness_digest(), "cases": rows,
              "actor_sessions": 0, "paid_api_calls": 0}
    (layout.run / "fixture-admission.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
    return 0 if all(row["status"] == "admitted" for row in rows) else EXIT_REFUSED


def main(argv: Sequence[str] | None = None) -> int:
    from .executor import CALL_BUDGET, SHAPES, TOOL_SEARCH_DEFAULT
    from .resolve import RESOLVERS

    parser = argparse.ArgumentParser(
        prog="python -m membench.utility.tool_selection", description=__doc__.splitlines()[0]
    )
    sub = parser.add_subparsers(dest="command", required=True)
    shapes = ",".join(SHAPES)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--cases", help="a case file (default: the committed dev set)")
        p.add_argument("--case-ids", help="comma-separated subset of the case file")
        p.add_argument("--arms", default=shapes, help=f"comma-separated tool shapes (default: {shapes})")
        p.add_argument("--repeats", type=int, default=DEFAULT_REPEATS)

    p = sub.add_parser("plan", help="session count and token estimate per round; starts nothing")
    common(p)
    p.add_argument(
        "--calibration", help="a calibration file from the calibrate stage (default: the committed one)"
    )
    p.add_argument(
        "--schema-bytes",
        action="append",
        help="ARM=BYTES of an arm's tool schemas, when known (repeatable)",
    )
    p.set_defaults(func=cmd_plan)
    p.add_argument("--heldout-count", type=int, action="append", help="an explicit prospective count; no held-out cases are generated")

    p = sub.add_parser("run", help="seed, probe and play every session")
    common(p)
    p.add_argument("--run", required=True)
    p.add_argument("--actor-model", required=True, help="the pinned actor model ID")
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--tool-search", default=TOOL_SEARCH_DEFAULT, help="ENABLE_TOOL_SEARCH for the client")
    p.add_argument("--call-budget", type=int, default=CALL_BUDGET)
    p.add_argument("--claude", help="the agent CLI executable (default: the first on PATH)")
    p.add_argument("--claude-launcher", help="a command that execs the client, called as LAUNCHER CLIENT -- ARGS")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--max-sessions", type=int, help="stop after this many new sessions")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("readout", help="score a run and compare each arm with grouped")
    p.add_argument("--run", required=True)
    p.add_argument("--resolver", choices=tuple(RESOLVERS), default="product")
    p.add_argument("--delta", type=float, default=protocol.DELTA_FLOOR)
    p.add_argument("--seed", type=int, default=0)
    p.set_defaults(func=cmd_readout)

    p = sub.add_parser("calibrate", help="write the numbers plan estimates from, from a finished run")
    p.add_argument("--run", required=True)
    p.add_argument("--resolver", choices=tuple(RESOLVERS), default="product")
    p.add_argument("--out", required=True)
    p.add_argument("--source", help="a short description of the run, recorded in the file")
    p.set_defaults(func=cmd_calibrate)

    p = sub.add_parser("generate", help="draw the held-out case file (once, after the dev round)")
    p.add_argument("--count", type=int, required=True)
    p.add_argument("--master-seed", type=int, required=True)
    p.add_argument("--out", required=True)
    p.set_defaults(func=cmd_generate)

    p = sub.add_parser("seed-vault", help=argparse.SUPPRESS)
    p.add_argument("--case-json", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--snapshot-out")
    p.set_defaults(func=cmd_seed_vault)

    p = sub.add_parser("snapshot-vault", help=argparse.SUPPRESS)
    p.add_argument("--vault", required=True)
    p.add_argument("--out", required=True)
    p.set_defaults(func=cmd_snapshot_vault)

    p = sub.add_parser("admit", help="materialize and check fixtures without starting actors")
    common(p)
    p.add_argument("--run", required=True)
    p.set_defaults(func=cmd_admit)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
