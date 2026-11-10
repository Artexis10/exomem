"""Replay product incidents on real models, one fresh session per run, and compare code versions.

Each file under ``cases/`` holds one incident's cases. A case seeds one note through the arm's own
writer, sends one user turn to Claude Code in print mode, and checks the outcome:

- ``seed_corrected``: the seeded note's body changed, or the arm's own status registry reports it
  superseded (a correction through either route counts);
- ``called_any``: the session called at least one of the named Exomem tools;
- ``judge``: a blind model fills the case's JSON schema from the user turn and the answer, and every
  ``require`` field must match.

An arm is a checkout: its ``src`` runs the MCP server, its ``plugins/claude-code`` supplies the skill
and hooks, and its own docs and code supply the guidance text, in native MCP activation mode. Every
session gets its own vault, state and hook home under a neutral unit id; the arms run interleaved in
one window, and the arm of a unit is recorded only in ``results.jsonl``.

A unit passes (true), fails (false) or is unknown (null): a harness fault, a seeding error or a
judge error is unknown, never a failure.

    PYTHONPATH=benchmarks:src python -m membench.outcome.incidents \\
        --arm base=../exomem-base --arm changed=. --repeats 3 --run-root /var/tmp/exomem-outcome-evals/runs/incidents-<id>
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import time
import uuid
from collections import Counter
from pathlib import Path

from epistemic.journeys.f27_replay import (
    DEFAULT_MAX_TURNS,
    Arm,
    JourneySetupError,
    build_turn_argv,
    count_hook_activity,
    discover_agent_envelope,
    fault_reason,
    refuse_unsafe_out_dir,
)

from membench.trackc.witness_join import parse_stream_json_transcript

from .. import billing
from ..faults import init_faults
from ..isolation import (
    ACTOR_MODEL,
    SHIPPED_PROMINENCE,
    child_env,
    claude_env,
    hook_env,
    lay_instruction_block,
    mcp_server,
    plugin_read_allow,
)
from ..judging import Judge, JudgeSpec, grade_item
from ..record import checkout_state

CASES_DIR = Path(__file__).parent / "cases"
ARM_SIDE = Path(__file__).with_name("arm_side.py")
ARM_SIDE_TIMEOUT_SECONDS = 300
SESSION_TIMEOUT_SECONDS = 900
JUDGE_TIMEOUT_SECONDS = 300
#: The checks this runner implements; a case's `pass` names a subset.
CHECKS = ("seed_corrected", "called_any", "judge")
JUDGE_PROMPT = (
    "You grade one assistant answer. Read the user's turn and the answer, then fill the schema.\n\n"
    "User turn: {turn}\n\nAnswer:\n<<<\n{answer}\n>>>"
)


def load_cases(selected: list[str] | None) -> list[dict]:
    cases = [
        case
        for path in sorted(CASES_DIR.glob("*.json"))
        for case in json.loads(path.read_text(encoding="utf-8"))["cases"]
    ]
    for case in cases:
        # A misspelled rule would leave a case with no checks, and it would pass every run.
        unknown = set(case["pass"]) - set(CHECKS)
        if unknown or not case["pass"]:
            raise SystemExit(
                f"case {case['id']}: pass rules must be among {CHECKS}, got {sorted(unknown)}"
            )
        spec = case["pass"].get("judge")
        # An empty `require` would pass every answer, and a field the schema does not require may be absent.
        if spec and (
            not spec["require"] or set(spec["require"]) - set(spec["schema"].get("required", ()))
        ):
            raise SystemExit(
                f"case {case['id']}: judge `require` must be non-empty and within the schema's `required`"
            )
    unknown_ids = set(selected or ()) - {case["id"] for case in cases}
    if unknown_ids:
        raise SystemExit(
            f"no case with id {sorted(unknown_ids)}; the cases are {[case['id'] for case in cases]}"
        )
    return [case for case in cases if not selected or case["id"] in selected]


def run_arm_side(arm: Path, root: Path, *args: str, stdin: str | None = None) -> dict:
    """Run ``arm_side.py`` as the arm's own product code under the session's pins; its one JSON line."""
    done = subprocess.run(  # noqa: S603 - fixed argv
        [sys.executable, str(ARM_SIDE), *args],
        input=stdin,
        env=child_env(root, arm),
        capture_output=True,
        text=True,
        timeout=ARM_SIDE_TIMEOUT_SECONDS,
    )
    if done.returncode != 0:
        raise RuntimeError(f"arm_side {args[0]} failed: {done.stderr.strip()[-300:]}")
    return json.loads(done.stdout.strip().splitlines()[-1])


def seed_corrected(before: dict, after: dict) -> bool | None:
    """Whether the seeded note was corrected: its body changed, or the product reports it superseded.

    A removed note counts as changed. None when the body is unchanged and the product's status
    registry could not classify the note, because then supersession is unknown.
    """
    if after["body"] != before["body"] or after["lifecycle_class"] == "superseded":
        return True
    return None if after["lifecycle_class"] is None else False


def settle(checks: dict[str, bool | None]) -> bool | None:
    """Every check true passes, any check false fails, and an unknown with no failure is unknown."""
    if False in checks.values():
        return False
    return None if None in checks.values() else True


def init_event(stdout: str) -> dict:
    """The session's ``init`` event, or an empty dict when the stream has none."""
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if (
            isinstance(event, dict)
            and event.get("type") == "system"
            and event.get("subtype") == "init"
        ):
            return event
    return {}


def play(case: dict, arm: Path, root: Path, *, executable: Path, model: str, max_turns: int):
    """Seed the arm's vault and guidance, then run one session. Returns (seeded note, finished process, record)."""
    work, block = root / "work", root / "custom-instructions.txt"
    plugin = arm / "plugins" / "claude-code"
    work.mkdir(parents=True)
    mcp = root / "mcp.json"
    mcp.write_text(json.dumps({"mcpServers": {"exomem": mcp_server(root, arm)}}))
    seeded = run_arm_side(
        arm, root, "seed", str(root / "vault"), str(work), stdin=json.dumps(case["seed"])
    )
    record = {"instruction_block": lay_instruction_block(block, arm)}
    actor = Arm(
        arm_id="incident",
        prominence=SHIPPED_PROMINENCE,
        tools="Read,Skill",
        uses_plugin=True,
        allowed_tools=",".join(("mcp__exomem", "Skill", "Read(./**)", plugin_read_allow(plugin))),
        uses_custom_instructions=True,
        configure_ref="membench.outcome.incidents",
    )
    argv = build_turn_argv(
        executable=executable,
        arm=actor,
        prompt=case["prompt"],
        mcp_config=mcp,
        session_id=None,
        model=model,
        max_turns=max_turns,
        first=True,
        append_system_prompt_file=block,
        plugin_dir=plugin,
    )
    # The server's native MCP activation mode: the hooks route through the tools, not inject text.
    env = {**claude_env()[0], **hook_env(root), "EXOMEM_RETRIEVE_INJECT": "mcp"}
    start = time.monotonic()
    done = subprocess.run(  # noqa: S603 - argv from the shared turn builder
        argv, cwd=work, env=env, capture_output=True, text=True, timeout=SESSION_TIMEOUT_SECONDS
    )
    record["seconds"] = round(time.monotonic() - start, 1)
    record["claude_md_sha256"] = hashlib.sha256((work / "CLAUDE.md").read_bytes()).hexdigest()
    (root / "stream.jsonl").write_text(done.stdout)
    return seeded, done, record


def judge_answer(
    case: dict, answer: str, root: Path, judge: Judge
) -> tuple[bool | None, str | None]:
    """The judge check and, when it is not true or false on a verdict, the reason.

    An answer that is empty from a session that ran is a product failure and is not judged. A judge
    that gives no usable verdict leaves the check unknown.
    """
    if not answer:
        return False, "the answer is empty"
    spec = case["pass"]["judge"]
    row = grade_item(
        judge,
        root.name,
        JUDGE_PROMPT.format(turn=case["prompt"], answer=answer),
        spec["schema"],
        out_dir=root,
        pass_name="verdicts",
        timeout=JUDGE_TIMEOUT_SECONDS,
        problem=lambda v: (
            None if set(spec["require"]) <= set(v) else "the verdict lacks a required field"
        ),
    )
    (root / "verdict.json").write_text(json.dumps(row, indent=2))
    if row["verdict"] is None:
        return None, f"judge error: {row['errors'][-1][:300]}"
    return all(row["verdict"].get(k) == v for k, v in spec["require"].items()), None


def run_unit(case: dict, arm: Path, root: Path, *, judge: Judge, **session) -> dict:
    """One fresh session. `passed` is true, false or null; null always comes with a `reason`."""
    seeded, done, record = play(case, arm, root, **session)
    transcript = parse_stream_json_transcript(done.stdout.splitlines())
    init = init_event(done.stdout)
    record |= {
        "notional_usd": transcript.total_cost_usd,
        "model_reported": init.get("model"),
        "api_key_source": init.get("apiKeySource"),
        "billing": billing.of_key_source(init.get("apiKeySource")),
    }
    fault = fault_reason(done, transcript) or "; ".join(
        init_faults(init, model=session["model"], exomem=True)
    )
    if fault:
        return {**record, "passed": None, "reason": f"harness fault: {fault}"}
    after = run_arm_side(arm, root, "probe", str(root / "vault"), seeded["path"])
    # `mcp__exomem__` is the tool-name prefix that the "exomem" server name in the MCP config fixes.
    tools = [use.name.removeprefix("mcp__exomem__") for use in transcript.memory_tool_uses()]
    rule, checks, reasons = case["pass"], {}, []
    if "seed_corrected" in rule:
        corrected = seed_corrected(seeded, after)
        checks["seed_corrected"] = (
            None if corrected is None else corrected == rule["seed_corrected"]
        )
        if corrected is None:
            reasons.append("the arm's status registry could not classify the seeded note")
    if "called_any" in rule:
        checks["called_any"] = bool(set(tools) & set(rule["called_any"]))
    if "judge" in rule:
        checks["judge"], reason = judge_answer(case, transcript.result_text.strip(), root, judge)
        reasons += [reason] if reason else []
    invocations, firings = count_hook_activity(done.stdout)
    return record | {
        "passed": settle(checks),
        "reason": "; ".join(reasons) or None,
        "checks": checks,
        "tools": tools,
        "hook_invocations": invocations,
        "capture_nudge_firings": firings,
        "seed_after": {"exists": after["exists"], "lifecycle_class": after["lifecycle_class"]},
    }


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m membench.outcome.incidents")
    parser.add_argument("--arm", action="append", required=True, help="NAME=CHECKOUT, repeatable")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--case", action="append", help="case id to run; default all")
    parser.add_argument("--model", default=ACTOR_MODEL)
    parser.add_argument("--max-turns", type=int, default=DEFAULT_MAX_TURNS)
    parser.add_argument("--run-root", type=Path, required=True)
    args = parser.parse_args(argv)
    arms = {name: Path(path).resolve() for name, path in (item.split("=", 1) for item in args.arm)}
    for name, path in arms.items():
        if (
            not (path / "src" / "exomem").is_dir()
            or not (path / "plugins" / "claude-code").is_dir()
        ):
            raise SystemExit(f"--arm {name}: {path} holds no src/exomem and plugins/claude-code")
    cases = load_cases(args.case)
    try:
        run_root = refuse_unsafe_out_dir(args.run_root)
    except JourneySetupError as error:
        raise SystemExit(str(error)) from error
    if run_root.exists() and any(run_root.iterdir()):
        raise SystemExit(f"{run_root} already holds a run; use a new --run-root")
    (run_root / "judge").mkdir(parents=True)
    envelope = discover_agent_envelope()
    judge = Judge(JudgeSpec.of("claude", executable=str(envelope.executable)), run_root / "judge")
    manifest = {
        "cli": envelope.version,
        "actor": {"model_requested": args.model, "max_turns": args.max_turns},
        "judge": judge.spec.record(),
        "cases": [case["id"] for case in cases],
        "repeats": args.repeats,
        "arms": {
            name: {"checkout": str(path), **checkout_state(path)} for name, path in arms.items()
        },
    }
    (run_root / "manifest.json").write_text(json.dumps(manifest, indent=2))
    tally: dict[tuple[str, str], Counter] = {}
    for rep in range(1, args.repeats + 1):
        for case in cases:
            for name in list(arms) if rep % 2 else list(reversed(arms)):
                # A neutral id: no session or judge directory carries the arm's name.
                unit = uuid.uuid4().hex[:12]
                try:
                    record = run_unit(
                        case,
                        arms[name],
                        run_root / "units" / unit,
                        judge=judge,
                        executable=envelope.executable,
                        model=args.model,
                        max_turns=args.max_turns,
                    )
                except Exception as error:  # noqa: BLE001 - a runner or seeding error is unknown, never a product failure
                    record = {"passed": None, "reason": f"error: {error!r:.400}"}
                record = {"unit": unit, "case": case["id"], "arm": name, "rep": rep, **record}
                with (run_root / "results.jsonl").open("a") as handle:
                    handle.write(json.dumps(record) + "\n")
                tally.setdefault((case["id"], name), Counter())[record["passed"]] += 1
                print(json.dumps(record), flush=True)
    for (case_id, name), counts in sorted(tally.items()):
        print(
            f"{case_id}\t{name}\t{counts[True]} passed\t{counts[False]} failed\t{counts[None]} unknown"
        )


if __name__ == "__main__":
    main()
