"""The switch evaluation's stages, one subcommand each.

Run from the repository root with the benchmarks on the path::

    PYTHONPATH=benchmarks uv run python -m membench.utility.switch_eval <stage> ...

1. ``synthetic`` builds the synthetic vaults and writes their key and case files.
2. ``prepare`` records the run's inputs and plan in the run directory.
3. ``state`` builds each arm's state once per tree and captures its surface.
4. ``run`` plays every session; ``run --dry-run`` prints them and runs nothing.
5. ``judge`` checks the chain, writes the blind items and runs one judge route.
6. ``score`` unblinds, applies the tie-break when two routes judged, and scores.
7. ``power`` runs the power step on a scored development run.

Exit codes: 0 done, 2 refused or aborted, 3 run void, 75 paused on a quota.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from collections.abc import Sequence
from dataclasses import asdict
from pathlib import Path

from . import protocol

EXIT_REFUSED = 2
EXIT_VOID = 3
EXIT_PAUSED = 75


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _harness_digest() -> str:
    """Digest of the harness's own code and texts, recorded with the inputs."""

    root = Path(__file__).resolve().parent
    digest = hashlib.sha256()
    for path in sorted([*root.glob("*.py"), *(root / "texts").glob("*.md")]):
        digest.update(path.name.encode() + b"\0" + path.read_bytes() + b"\0")
    return digest.hexdigest()


#: The stages whose records a chain check reads: each must have run on the
#: harness code that prepare recorded.
_STAGES_BEFORE_JUDGING = ("state", "run")


def _stage(layout, inputs: dict, stage: str | None) -> None:
    """Refuse a stage whose harness code is not the code prepare recorded, then
    record the digest the stage ran on; ``stage`` ``None`` checks only."""

    from datetime import UTC, datetime

    digest = _harness_digest()
    if digest != inputs["harness_digest"]:
        raise SystemExit(
            f"refused: the harness code is {digest[:16]}, the run was prepared on "
            f"{inputs['harness_digest'][:16]}; prepare a new run directory"
        )
    if stage is None:
        return
    record = {
        "stage": stage,
        "harness_digest": digest,
        "at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    with open(layout.run / "stages.jsonl", "a", encoding="utf-8") as log:
        log.write(json.dumps(record, sort_keys=True) + "\n")


def _stage_problems(layout, inputs: dict, needed: Sequence[str]) -> list[str]:
    path = layout.run / "stages.jsonl"
    records = (
        [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
        if path.exists()
        else []
    )
    problems = [
        f"the {r['stage']} stage ran on harness {r['harness_digest'][:16]}, "
        f"not prepare's {inputs['harness_digest'][:16]}"
        for r in records
        if r["harness_digest"] != inputs["harness_digest"]
    ]
    ran = {r["stage"] for r in records}
    problems += [
        f"no harness digest recorded for the {stage} stage" for stage in needed if stage not in ran
    ]
    return problems


def _pairs(values: Sequence[str], what: str) -> list[tuple[str, str]]:
    pairs = []
    for value in values:
        name, sep, rest = value.partition("=")
        if not sep or not name or not rest:
            raise SystemExit(f"{what} takes NAME=PATH, got {value!r}")
        pairs.append((name, rest))
    return pairs


def _refuse_api_key(env) -> None:
    from .session import API_KEY_VARIABLES

    present = [name for name in API_KEY_VARIABLES if env.get(name)]
    if present:
        raise SystemExit(
            f"refused: {', '.join(present)} is set; the actor runs on the subscription with no API key in the environment"
        )


def _load(run: Path, name: str):
    path = run / name
    if not path.exists():
        raise SystemExit(f"{path} is missing: run the stage that writes it first")
    return json.loads(path.read_text(encoding="utf-8"))


def _as_json(value: object) -> object:
    """``value`` as it reads back from its JSON file, so a stored record compares equal."""

    return json.loads(json.dumps(value, sort_keys=True))


def _inputs_digest(inputs: dict) -> str:
    return hashlib.sha256(json.dumps(inputs, sort_keys=True).encode()).hexdigest()


# --------------------------------------------------------------------------


def cmd_synthetic(args: argparse.Namespace) -> int:
    from .cases import synthetic_inputs

    case_ids = args.cases.split(",") if args.cases else None
    key, cases, trees = synthetic_inputs(Path(args.out), case_ids=case_ids)
    print(f"key:   {key}")
    print(f"cases: {cases}")
    for name, vault in sorted(trees.items()):
        print(f"tree {name}: {vault}")
    print("prepare with:")
    print(
        f"  --key synthetic={key} --cases {cases} "
        + " ".join(f"--tree {n}={v}" for n, v in sorted(trees.items()))
    )
    return 0


def cmd_prepare(args: argparse.Namespace) -> int:
    from epistemic.journeys.f27_replay import (
        AgentEnvelope,
        declared_cli_options,
        discover_agent_envelope,
    )

    from .arms import arm_specs, profile_settings
    from .cases import load_cases
    from .session import arm_files, plan_sessions, session_argv
    from .state import Layout, tree_digest

    _refuse_api_key(os.environ)
    layout = Layout.at(Path(args.run))
    arm_ids = tuple(args.arms.split(","))
    unknown = [arm for arm in arm_ids if arm not in protocol.ARM_IDS]
    if unknown or len(set(arm_ids)) != len(arm_ids):
        raise SystemExit(f"--arms takes distinct arms from {protocol.ARM_IDS}, got {args.arms}")
    keys = [(split, Path(path)) for split, path in _pairs(args.key, "--key")]
    case_files = [Path(path) for path in args.cases]
    cases, struck = load_cases(keys=keys, case_files=case_files)
    if args.case_ids:
        wanted = args.case_ids.split(",")
        cases = tuple(case for case in cases if case.case_id in wanted)
        if len(cases) != len(set(wanted)):
            raise SystemExit("--case-ids names a case the key does not hold")
    trees = {}
    for name, value in _pairs(args.tree, "--tree"):
        path, _, frozen = value.partition("@")
        # The digest the freeze recorded, when given, must be the copy's; else
        # the copy's digest now, so the chain check sees any later change.
        digest = tree_digest(Path(path))
        if frozen and frozen != digest:
            raise SystemExit(f"tree {name}: the copy's digest {digest} is not the frozen {frozen}")
        trees[name] = {"path": str(Path(path).resolve()), "digest": digest}
    missing = sorted({case.tree for case in cases} - set(trees))
    if missing:
        raise SystemExit(f"cases run on trees with no --tree: {', '.join(missing)}")

    if args.claude:
        import subprocess

        version = subprocess.run(
            [args.claude, "--version"], capture_output=True, text=True, check=True
        ).stdout.strip()
        envelope = AgentEnvelope(executable=Path(args.claude), version=version)
    else:
        envelope = discover_agent_envelope()
    # The native binary itself, not a link an update moves: the run pins it.
    envelope = AgentEnvelope(executable=envelope.executable.resolve(), version=envelope.version)
    import shlex

    launcher = shlex.split(args.claude_launcher or "")
    declared = declared_cli_options(envelope)
    specs = arm_specs(hook_mode=args.hook_mode, profile=args.profile)
    undeclared = sorted(
        {
            token
            for arm in arm_ids
            for token in session_argv(
                envelope, specs[arm], arm_files(layout, specs[arm]), model=args.actor_model
            )
            if token.startswith("--") and token not in declared
        }
    )
    if undeclared:
        raise SystemExit(f"the CLI's help does not declare {', '.join(undeclared)}")

    inputs = _as_json(
        {
            "design": protocol.DESIGN,
            "protocol": {
                name: getattr(protocol, name)
                for name in dir(protocol)
                if name.isupper() and isinstance(getattr(protocol, name), (int, float, str, tuple))
            },
            "harness_digest": _harness_digest(),
            "actor_model": args.actor_model,
            "client": {
                "executable": str(envelope.executable),
                "version": envelope.version,
                "launcher": launcher,
            },
            "arms": list(arm_ids),
            "hook_mode": args.hook_mode,
            "retrieval_profile": {"name": args.profile, "settings": profile_settings(args.profile)},
            "repeats": args.repeats,
            "seed": args.seed,
            "keys": [
                {"split": split, "path": str(path), "sha256": _sha256_file(path)}
                for split, path in keys
            ],
            "case_files": [
                {"path": str(path), "sha256": _sha256_file(path)} for path in case_files
            ],
            "case_ids": [case.case_id for case in cases],
            "struck_before_freeze": [s.case_id for s in struck],
            "trees": trees,
        }
    )
    existing = layout.run / "inputs.json"
    if existing.exists() and json.loads(existing.read_text(encoding="utf-8")) != inputs:
        raise SystemExit(
            f"{existing} holds other inputs; a run keeps its inputs (use a new run directory)"
        )
    layout.run.mkdir(parents=True, exist_ok=True)
    plan = plan_sessions(cases, arm_ids=arm_ids, repeats=args.repeats, seed=args.seed)
    existing.write_text(json.dumps(inputs, indent=2, sort_keys=True), encoding="utf-8")
    (layout.run / "cases.json").write_text(
        json.dumps([c.as_dict() for c in cases], indent=2), encoding="utf-8"
    )
    (layout.run / "plan.json").write_text(
        json.dumps([asdict(p) for p in plan], indent=2), encoding="utf-8"
    )
    print(
        f"prepared {layout.run}: {len(cases)} cases, arms {','.join(arm_ids)}, {args.repeats} repeats, {len(plan)} sessions"
    )
    print(f"client {envelope.executable} {envelope.version}; actor model {args.actor_model}")
    print(f"launched through: {shlex.join(launcher) or 'nothing (the client itself)'}")
    return 0


def _context(run: Path):
    from .arms import arm_specs
    from .cases import Case
    from .state import Layout

    layout = Layout.at(run)
    inputs = _load(layout.run, "inputs.json")
    cases = [Case.from_dict(c) for c in _load(layout.run, "cases.json")]
    plan = _load(layout.run, "plan.json")
    specs = arm_specs(hook_mode=inputs["hook_mode"], profile=inputs["retrieval_profile"]["name"])
    return layout, inputs, cases, plan, specs


def _surface(layout, arm: str, trees: Sequence[str]) -> dict | None:
    """The arm's captured surface, the same on every tree, or ``None`` before the state stage."""

    from .state import load_arm_state

    surfaces = [load_arm_state(layout, arm, tree) for tree in trees]
    if any(s is None for s in surfaces):
        return None
    distinct = {json.dumps(s["surface"], sort_keys=True) for s in surfaces}
    if len(distinct) != 1:
        raise SystemExit(
            f"arm {arm}: the server surface differs between trees; the shipped text must be one"
        )
    return surfaces[0]["surface"]


def _arm_hashes(layout, inputs, specs, trees) -> dict:
    from .arms import arm_hashes
    from .state import Slot

    slot = Slot(layout.slot)
    return {
        arm: arm_hashes(specs[arm], hook_home=slot.hook_home, surface=_surface(layout, arm, trees))
        for arm in inputs["arms"]
    }


def cmd_state(args: argparse.Namespace) -> int:
    from .state import build_arm

    layout, inputs, cases, _plan, specs = _context(Path(args.run))
    _stage(layout, inputs, "state")
    needed = sorted({case.tree for case in cases})
    for arm in inputs["arms"]:
        for tree in needed:
            source = Path(inputs["trees"][tree]["path"])
            record = build_arm(
                layout, specs[arm], tree=tree, source_vault=source, parent_env=os.environ
            )
            print(
                f"arm {arm} tree {tree}: state {record['state_digest'][:16]} vault {record['vault_digest'][:16]} ({record['build_seconds']} s)"
            )
    hashes = _arm_hashes(layout, inputs, specs, needed)
    (layout.run / "arm-hashes.json").write_text(
        json.dumps(hashes, indent=2, sort_keys=True), encoding="utf-8"
    )
    for arm, record in hashes.items():
        print(f"arm {arm}: arm hash {record['arm_hash']}")
    return 0


def cmd_build_state(args: argparse.Namespace) -> int:
    from .state import build_state

    print(json.dumps(build_state(Path(args.vault))))
    return 0


def _session_inputs_for(layout, inputs, cases, specs, planned: dict, parent_env):
    from epistemic.journeys.f27_replay import AgentEnvelope

    from .session import PlannedSession, session_inputs
    from .state import load_arm_state

    case = next(c for c in cases if c.case_id == planned["case_id"])
    record = load_arm_state(layout, planned["arm"], case.tree)
    digests = (
        {"vault": record["vault_digest"], "state": record["state_digest"]}
        if record is not None
        else {"vault": "pending: state stage not run", "state": "pending: state stage not run"}
    )
    envelope = AgentEnvelope(
        executable=Path(inputs["client"]["executable"]), version=inputs["client"]["version"]
    )
    return session_inputs(
        layout=layout,
        planned=PlannedSession(**planned),
        case=case,
        spec=specs[planned["arm"]],
        envelope=envelope,
        model=inputs["actor_model"],
        parent_env=parent_env,
        state_digests=digests,
    )


def cmd_run(args: argparse.Namespace) -> int:
    from .session import (
        HARNESS_FAULT,
        STRUCK,
        ClaudeSessionExecutor,
        RunAborted,
        RunPaused,
        arm_files,
        dry_run_lines,
        load_session,
        run_session,
        write_arm_files,
    )

    layout, inputs, cases, plan, specs = _context(Path(args.run))
    _stage(layout, inputs, None if args.dry_run else "run")
    parent = dict(os.environ)
    _refuse_api_key(parent)
    trees = sorted({case.tree for case in cases})
    hashes = _arm_hashes(layout, inputs, specs, trees)

    if args.dry_run:
        print(f"dry run of {layout.run}: {len(plan)} sessions, nothing is started or written")
        print(
            f"client {inputs['client']['executable']} {inputs['client']['version']}; actor model {inputs['actor_model']}"
        )
        print(f"launched through: {' '.join(inputs['client']['launcher']) or 'nothing'}")
        print(
            f"account home: CLAUDE_CONFIG_DIR={parent.get('CLAUDE_CONFIG_DIR', '(unset: the CLI default)')}"
        )
        for arm, record in hashes.items():
            print(f"arm {arm}: arm hash {record['arm_hash']}")
            print(
                f"  client instructions from {record['client_instructions_source']}; hook mode {record['hook_mode'] or 'none'}"
            )
            for name, value in record["components"].items():
                print(f"  {name}: {value}")
        for planned in plan:
            for line in dry_run_lines(
                _session_inputs_for(layout, inputs, cases, specs, planned, parent),
                parent_env=parent,
            ):
                print(line)
        return 0

    pending = [arm for arm, record in hashes.items() if record["arm_hash"].startswith("pending")]
    if pending:
        print(
            f"refused: arms {', '.join(pending)} have no built state; run the state stage first",
            file=sys.stderr,
        )
        return EXIT_REFUSED
    frozen = layout.run / "arm-hashes.json"
    if not frozen.exists() or json.loads(frozen.read_text(encoding="utf-8")) != _as_json(hashes):
        print("refused: the arm hashes moved since the state stage", file=sys.stderr)
        return EXIT_REFUSED
    import subprocess

    version = subprocess.run(
        [inputs["client"]["executable"], "--version"], capture_output=True, text=True, check=True
    ).stdout.strip()
    if version != inputs["client"]["version"]:
        print(
            f"refused: the client is {version}, the run pinned {inputs['client']['version']}",
            file=sys.stderr,
        )
        return EXIT_REFUSED
    from .state import load_arm_state

    surfaces = {
        (arm, tree): load_arm_state(layout, arm, tree)["surface"]
        for arm in inputs["arms"]
        for tree in trees
    }
    client_version = version.split()[0]
    executor = ClaudeSessionExecutor(
        layout=layout,
        specs=specs,
        model=inputs["actor_model"],
        client_version=client_version,
        surfaces=surfaces,
        launcher=tuple(inputs["client"]["launcher"]),
    )
    struck: set[str] = set()
    limit = args.max_sessions or len(plan)
    ran = 0
    for planned in plan:
        session_dir = layout.session_dir(planned["case_id"], planned["arm"], planned["repeat"])
        record = load_session(session_dir)
        if record is not None:
            if record["outcome"] in (HARNESS_FAULT, STRUCK):
                struck.add(record["case_id"])
            continue
        if planned["case_id"] in struck:
            session_dir.mkdir(parents=True, exist_ok=True)
            (session_dir / "session.json").write_text(
                json.dumps(
                    {
                        **planned,
                        "outcome": STRUCK,
                        "kind": "case struck after harness faults",
                        "attempts": 0,
                        "turns": [],
                        "answer": None,
                        "checks": {},
                    },
                    indent=2,
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
            continue
        if len(struck) > protocol.MAX_STRUCK_CASES:
            print(
                f"run void: {len(struck)} cases struck ({', '.join(sorted(struck))})",
                file=sys.stderr,
            )
            return EXIT_VOID
        if ran >= limit:
            break
        write_arm_files(arm_files(layout, specs[planned["arm"]]))
        inputs_for = _session_inputs_for(layout, inputs, cases, specs, planned, parent)
        try:
            record = run_session(inputs_for, session_dir, executor)
        except RunPaused as pause:
            print(f"paused: {pause}; rerun this stage to resume", file=sys.stderr)
            return EXIT_PAUSED
        except RunAborted as abort:
            print(f"aborted: {abort}", file=sys.stderr)
            return EXIT_REFUSED
        ran += 1
        if record["outcome"] == HARNESS_FAULT:
            struck.add(record["case_id"])
        print(
            f"{planned['session_id']}: {record['outcome']} {record['kind']} ({record['attempts']} attempts)"
        )
    if len(struck) > protocol.MAX_STRUCK_CASES:
        print(f"run void: {len(struck)} cases struck", file=sys.stderr)
        return EXIT_VOID
    return 0


def _chain_before_judging(layout, inputs, cases, plan, specs):
    from .score import check_sessions, check_trees

    trees = sorted({case.tree for case in cases})
    hashes = json.loads((layout.run / "arm-hashes.json").read_text(encoding="utf-8"))
    if hashes != _as_json(_arm_hashes(layout, inputs, specs, trees)):
        raise SystemExit("chain check: the arm hashes moved since the state stage")
    problems = check_trees(layout, cases=cases, trees=inputs["trees"], arm_ids=inputs["arms"])
    session_problems, sessions = check_sessions(layout, plan=plan, arm_hashes=hashes)
    problems += session_problems
    problems += _stage_problems(layout, inputs, _STAGES_BEFORE_JUDGING)
    if problems:
        raise SystemExit("chain check failed before judging:\n  " + "\n  ".join(problems))
    return sessions


def cmd_judge(args: argparse.Namespace) -> int:
    from .judge import JudgeError, build_items, judge_agreement, run_judge, write_items
    from .score import check_judge_inputs

    layout, inputs, cases, plan, specs = _context(Path(args.run))
    _stage(layout, inputs, None if args.items_only else "judge")
    sessions = _chain_before_judging(layout, inputs, cases, plan, specs)
    digest = _inputs_digest(inputs)
    if not (layout.judge / "items.jsonl").exists():
        write_items(
            layout,
            build_items(cases, list(sessions.values()), seed=inputs["seed"], inputs_digest=digest),
        )
    problems = check_judge_inputs(
        layout,
        cases=cases,
        sessions=list(sessions.values()),
        seed=inputs["seed"],
        inputs_digest=digest,
    )
    if problems:
        raise SystemExit("chain check failed: " + "; ".join(problems))
    if args.items_only:
        print(f"items written to {layout.judge / 'items.jsonl'}")
        return 0
    try:
        result = run_judge(
            layout,
            route=args.route,
            model=args.judge_model,
            effort=args.judge_effort,
            claude=inputs["client"]["executable"],
            launcher=inputs["client"]["launcher"],
        )
    except JudgeError as error:
        print(f"paused: {error}; rerun this stage to resume", file=sys.stderr)
        return EXIT_PAUSED
    report = judge_agreement(layout, args.route)
    print(json.dumps({**result, "agreement": report}, indent=2))
    if report["recovery_required"]:
        print(
            "required-item agreement is below 0.8: run the other route over every item (recovery path)"
        )
    return 0


def _score(layout, inputs, cases, plan, specs, args) -> dict:
    from .judge import UnblindingError, resolved_verdicts, unblind
    from .score import check_sessions, score_run

    routes = args.routes.split(",")
    problems = _stage_problems(layout, inputs, (*_STAGES_BEFORE_JUDGING, "judge"))
    if problems:
        raise SystemExit("chain check failed after judging:\n  " + "\n  ".join(problems))
    try:
        unblinded = unblind(layout, routes=routes)
    except UnblindingError as error:
        raise SystemExit(f"chain check failed after judging: {error}") from error
    hashes = json.loads((layout.run / "arm-hashes.json").read_text(encoding="utf-8"))
    _problems, sessions = check_sessions(layout, plan=plan, arm_hashes=hashes)
    verdicts = resolved_verdicts(layout, unblinded)
    return score_run(
        layout,
        cases=cases,
        sessions=sessions,
        verdicts=verdicts,
        arm_ids=inputs["arms"],
        repeats=inputs["repeats"],
        split=args.split,
        deltas={2: args.delta_rule2, 3: args.delta_rule3},
        seed=inputs["seed"],
        project_sessions=args.project_sessions,
    )


def cmd_score(args: argparse.Namespace) -> int:
    layout, inputs, cases, plan, specs = _context(Path(args.run))
    _stage(layout, inputs, None if args.check else "score")
    result = _score(layout, inputs, cases, plan, specs, args)
    target = layout.score / "score.json"
    text = json.dumps(result, indent=2, sort_keys=True, default=str)
    if args.check:
        if not target.exists():
            raise SystemExit(f"{target} is missing: nothing to check")
        if target.read_text(encoding="utf-8") != text:
            print(
                "chain check failed: the stored score does not recompute from the raw verdicts",
                file=sys.stderr,
            )
            return EXIT_REFUSED
        print("chain check: the stored score recomputes from the raw verdicts")
        return 0
    layout.score.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    print(
        json.dumps(
            {k: result[k] for k in ("split", "diagnostic", "void", "struck", "decision")}, indent=2
        )
    )
    for rule in ("1", "2", "3", "4", "5"):
        verdict = result["rules"][rule]
        state = (
            "holds"
            if verdict["holds"]
            else ("fails" if verdict["evaluable"] else f"not evaluable: {verdict['reason']}")
        )
        print(f"rule {rule}: {state}")
    excluded = result["rules"]["5"].get("excluded_no_gold")
    if excluded:
        print(f"rule 5 excludes positives with no gold to lose: {', '.join(excluded)}")
    print(f"written to {target}")
    return 0


def cmd_power(args: argparse.Namespace) -> int:
    from .cases import KIND_MISS, KIND_ORDINARY, Case
    from .state import Layout
    from .stats import power_step, repeat_spread

    layout = Layout.at(Path(args.run))
    scored = _load(layout.score, "score.json")
    cases = [Case.from_dict(c) for c in _load(layout.run, "cases.json")]
    kinds = {KIND_ORDINARY} if args.rule == 2 else {KIND_MISS, KIND_ORDINARY}
    chosen = [
        c.case_id
        for c in cases
        if c.split == args.split and c.kind in kinds and c.case_id in scored["cases"]
    ]
    if not chosen:
        raise SystemExit("no scored development cases of this rule's kinds")
    repeat_scores = {
        c: {arm: row["repeats"] for arm, row in scored["cases"][c].items()} for c in chosen
    }
    rates = [
        sum(sum(scores) for scores in arms.values()) / sum(len(scores) for scores in arms.values())
        for arms in repeat_scores.values()
    ]
    sizes = [int(s) for s in args.sizes.split(",")] if args.sizes else []
    result = {
        "rule": args.rule,
        "repeat_spread": repeat_spread(repeat_scores),
        **power_step(
            rates,
            sizes=sizes,
            available=args.available,
            seed=args.seed,
            simulations=args.simulations,
        ),
    }
    print(json.dumps(result, indent=2))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    from .arms import (
        DEFAULT_HOOK_MODE,
        DEFAULT_PROFILE,
        HOOK_MODE_MCP,
        HOOK_MODE_WORKING_SET,
        RETRIEVAL_PROFILES,
    )

    parser = argparse.ArgumentParser(
        prog="python -m membench.utility.switch_eval", description=__doc__.splitlines()[0]
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("synthetic", help="build the synthetic vaults, key and case files")
    p.add_argument("--out", required=True)
    p.add_argument("--cases", help="comma-separated synthetic case ids (default: all)")
    p.set_defaults(func=cmd_synthetic)

    p = sub.add_parser("prepare", help="record the run's inputs and plan")
    p.add_argument("--run", required=True)
    p.add_argument(
        "--key", action="append", required=True, help="SPLIT=PATH of a key file (repeatable)"
    )
    p.add_argument(
        "--cases", action="append", required=True, help="a case file quoting the turns (repeatable)"
    )
    p.add_argument(
        "--tree",
        action="append",
        required=True,
        help="NAME=PATH[@DIGEST] of a vault copy (repeatable)",
    )
    p.add_argument("--actor-model", required=True, help="the pinned actor model ID")
    p.add_argument("--arms", default=",".join(protocol.ARM_IDS))
    p.add_argument("--repeats", type=int, default=protocol.REPEATS)
    p.add_argument("--seed", type=int, default=0)
    p.add_argument(
        "--hook-mode",
        default=DEFAULT_HOOK_MODE,
        choices=(HOOK_MODE_MCP, HOOK_MODE_WORKING_SET),
        help="the deployed retrieve hook's mode, shared by A and M",
    )
    p.add_argument(
        "--profile",
        default=DEFAULT_PROFILE,
        choices=tuple(RETRIEVAL_PROFILES),
        help="the retrieval profile every arm's server runs with",
    )
    p.add_argument("--case-ids", help="comma-separated subset of the key's cases")
    p.add_argument("--claude", help="the agent CLI executable (default: the first on PATH)")
    p.add_argument(
        "--claude-launcher",
        help="a command that execs the client, called as LAUNCHER CLIENT -- ARGS "
        "(a subscription's credential launcher); recorded with the run",
    )
    p.set_defaults(func=cmd_prepare)

    p = sub.add_parser("state", help="build each arm's state once per tree")
    p.add_argument("--run", required=True)
    p.set_defaults(func=cmd_state)

    p = sub.add_parser("build-state", help=argparse.SUPPRESS)
    p.add_argument("--vault", required=True)
    p.set_defaults(func=cmd_build_state)

    p = sub.add_parser("run", help="play every session")
    p.add_argument("--run", required=True)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument(
        "--max-sessions", type=int, help="stop after this many new sessions (the run resumes later)"
    )
    p.set_defaults(func=cmd_run)

    from .judge import ROUTES

    p = sub.add_parser("judge", help="check the chain, write blind items, run one judge route")
    p.add_argument("--run", required=True)
    p.add_argument("--route", choices=ROUTES, default=ROUTES[0])
    p.add_argument("--judge-model", help="the judge model ID (required unless --items-only)")
    p.add_argument("--judge-effort", help="the judge's reasoning effort")
    p.add_argument("--items-only", action="store_true")
    p.set_defaults(func=cmd_judge)

    p = sub.add_parser("score", help="unblind and score")
    p.add_argument("--run", required=True)
    p.add_argument(
        "--routes", default="codex", help="the judge routes whose verdicts stand, first route first"
    )
    p.add_argument(
        "--split",
        default=protocol.SPLIT_HELDOUT,
        help="the split the rules read; only the held-out split decides",
    )
    p.add_argument("--delta-rule2", type=float, default=protocol.DELTA_FLOOR)
    p.add_argument("--delta-rule3", type=float, default=protocol.DELTA_FLOOR)
    p.add_argument("--project-sessions", type=int)
    p.add_argument(
        "--check", action="store_true", help="recompute and compare with the stored score"
    )
    p.set_defaults(func=cmd_score)

    p = sub.add_parser("power", help="the power step on a scored development run")
    p.add_argument("--run", required=True)
    p.add_argument("--rule", type=int, choices=(2, 3), required=True)
    p.add_argument(
        "--available", type=int, required=True, help="held-out cases available for the rule"
    )
    p.add_argument(
        "--split", default=protocol.SPLIT_DEV, help="the split whose repeats give the spread"
    )
    p.add_argument("--sizes", help="comma-separated held-out sizes to try")
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--simulations", type=int, default=2000)
    p.set_defaults(func=cmd_power)

    args = parser.parse_args(argv)
    if args.command == "judge" and not args.items_only:
        # Dynamic choices with no default: the judge is chosen before judging starts.
        if not args.judge_model:
            parser.error("--judge-model is required to run a judge route")
        if args.route == "codex" and not args.judge_effort:
            parser.error(
                "--judge-effort is required on the codex route (the protocol's highest effort)"
            )
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
