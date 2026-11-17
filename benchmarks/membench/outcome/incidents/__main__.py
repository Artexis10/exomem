"""Replay product incidents on real models, one fresh session per run, and compare code versions.

Each file under ``cases/`` holds one incident's cases. A case seeds one note through the arm's own
writer, sends one user turn to Claude Code in print mode, and checks the outcome:

- ``seed_corrected``: the seeded note's body changed, or the arm's own status registry reports it
  superseded (a correction through either route counts);
- ``called_any``: the session called at least one of the named Exomem tools;
- ``judge``: a blind model fills the case's JSON schema from the user turn and the answer, and every
  ``require`` field must match. ``include_sources`` adds the Sources the session saved (title, kind, url,
  document form and identity check state); ``reference`` states what the case's files really are.
- ``source_saved``, ``source_count``, ``current_source``, ``compiled_with_backlinks``: the vault's Sources
  and the pages citing them, read by the arm's own code (``arm_side.py sources``), matched to the case's
  files by the SHA-256 of the stored original. ``current_source`` compares stored frontmatter fields
  exactly (``contains``, ``not_contains``, ``eq``, ``ne``; case-insensitive) on the Sources that no
  correction receipt supersedes; ``if_none`` is what it settles to when no such Source exists (false
  by default, because a case that expects a Source has failed when none is kept).

A case may declare ``files`` (built into the work directory at unit time, see ``fixtures.py``),
``seed_sources`` (captured as Sources before the session) and ``"transport": "http"``, which serves the
arm over loopback HTTP so the agent can send a file's bytes (``server.py``). Stdio is the default.

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
import os
import subprocess
import sys
import time
import uuid
from collections import Counter
from contextlib import nullcontext
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
    fresh_vault,
    hook_env,
    lay_instruction_block,
    mcp_server,
    plugin_read_allow,
)
from ..judging import Judge, JudgeSpec, grade_item
from ..record import checkout_state
from .fixtures import KINDS, build_files
from .server import lay_exomem_command, loopback_mcp

CASES_DIR = Path(__file__).parent / "cases"
ARM_SIDE = Path(__file__).with_name("arm_side.py")
ARM_SIDE_TIMEOUT_SECONDS = 300
SESSION_TIMEOUT_SECONDS = 900
JUDGE_TIMEOUT_SECONDS = 300
SOURCE_CHECKS = ("source_saved", "source_count", "current_source", "compiled_with_backlinks")
#: The checks this runner implements; a case's `pass` names a subset.
CHECKS = ("seed_corrected", "called_any", "judge", *SOURCE_CHECKS)
TRANSPORTS = ("stdio", "http")
#: The comparisons of a `current_source` condition: the check's own closed set, not a vocabulary.
CONDITIONS = {
    "contains": lambda text, wanted: wanted in text,
    "not_contains": lambda text, wanted: wanted not in text,
    "eq": lambda text, wanted: text == wanted,
    "ne": lambda text, wanted: text != wanted,
}
JUDGE_PROMPT = (
    "You grade one assistant answer. Read the user's turn and the answer, then fill the schema.\n\n"
    "User turn: {turn}\n\nAnswer:\n<<<\n{answer}\n>>>{extra}"
)


def named_files(rule: dict) -> list[str]:
    """Every file name that a case's source checks refer to."""
    return [
        *rule.get("source_saved", ()),
        *rule.get("compiled_with_backlinks", ()),
        *(rule[key]["file"] for key in ("source_count", "current_source") if key in rule),
    ]


def validate_case(case: dict) -> None:
    """Refuse a case that could pass or fail on nothing, before any session starts."""
    rule, files = case["pass"], case.get("files", {})
    # A misspelled rule would leave a case with no checks, and it would pass every run.
    unknown = set(rule) - set(CHECKS)
    if unknown or not rule:
        raise SystemExit(
            f"case {case['id']}: pass rules must be among {CHECKS}, got {sorted(unknown)}"
        )
    spec = rule.get("judge")
    # An empty `require` would pass every answer, and a field the schema does not require may be absent.
    if spec and (
        not spec["require"] or set(spec["require"]) - set(spec["schema"].get("required", ()))
    ):
        raise SystemExit(
            f"case {case['id']}: judge `require` must be non-empty and within the schema's `required`"
        )
    if case.get("transport", "stdio") not in TRANSPORTS:
        raise SystemExit(f"case {case['id']}: transport must be one of {TRANSPORTS}")
    if "seed" not in case and "seed_corrected" in rule:
        raise SystemExit(f"case {case['id']}: `seed_corrected` needs a `seed` note")
    if {file["kind"] for file in files.values()} - KINDS:
        raise SystemExit(f"case {case['id']}: file kinds must be among {sorted(KINDS)}")
    seeded = [source["file"] for source in case.get("seed_sources", ()) if "file" in source]
    # A misspelled file name matches no Source, so a check that settles on "no Source" would pass it.
    missing = sorted(set(named_files(rule) + seeded) - set(files))
    if missing:
        raise SystemExit(
            f"case {case['id']}: files {missing} are named but not declared in `files`"
        )
    for source in case.get("seed_sources", ()):
        if ("file" in source) == ("content" in source) or not {"title", "source_kind"} <= set(
            source
        ):
            raise SystemExit(
                f"case {case['id']}: a seed source has a title, a source_kind and one of file or content"
            )
    if "current_source" in rule:
        conditions = rule["current_source"].get("where", ())
        if not conditions or any(c["op"] not in CONDITIONS for c in conditions):
            raise SystemExit(
                f"case {case['id']}: current_source needs conditions with ops among {sorted(CONDITIONS)}"
            )
    if "source_count" in rule and type(rule["source_count"].get("equals")) is not int:
        raise SystemExit(f"case {case['id']}: source_count needs an integer `equals`")


def load_cases(selected: list[str] | None) -> list[dict]:
    cases = []
    for path in sorted(CASES_DIR.glob("*.json")):
        document = json.loads(path.read_text(encoding="utf-8"))
        # A case lists the files it uses by name; the file's `library` holds each spec once, for every case that shares it.
        library = document.get("library", {})
        for case in document["cases"]:
            absent = [name for name in case.get("files", ()) if name not in library]
            if absent:
                raise SystemExit(
                    f"case {case['id']}: files {absent} are not in {path.name}'s `library`"
                )
            cases.append({**case, "files": {name: library[name] for name in case.get("files", ())}})
    for case in cases:
        validate_case(case)
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


def holding(facts: dict, digest: str) -> list[dict]:
    """The Source rows whose stored original has this SHA-256."""
    return [row for row in facts["sources"] if row["original_sha256"] == digest]


def source_saved(facts: dict, fixtures: dict[str, str], names: list[str]) -> bool:
    """Every named file is held by at least one Source."""
    return all(holding(facts, fixtures[name]) for name in names)


def source_count(facts: dict, fixtures: dict[str, str], spec: dict) -> bool:
    """The named file's bytes are held by exactly ``equals`` Sources."""
    return len(holding(facts, fixtures[spec["file"]])) == spec["equals"]


def condition_holds(frontmatter: dict, condition: dict) -> bool:
    """One exact comparison on a stored frontmatter field; a missing field reads as empty text."""
    value = frontmatter.get(condition["field"])
    text = "" if value is None else str(value).casefold()
    return CONDITIONS[condition["op"]](text, str(condition["value"]).casefold())


def current_source(facts: dict, fixtures: dict[str, str], spec: dict) -> bool | None:
    """Every condition holds on each Source of the file that no correction receipt supersedes.

    When none remains the check settles to ``if_none`` (false unless the case says otherwise). None when
    the arm could not say whether a Source of the file was corrected.
    """
    held = holding(facts, fixtures[spec["file"]])
    if any(row["corrected"] is None for row in held):
        return None
    current = [row for row in held if not row["corrected"]]
    if not current:
        return spec.get("if_none", False)
    return all(condition_holds(row["frontmatter"], c) for row in current for c in spec["where"])


def compiled_with_backlinks(facts: dict, fixtures: dict[str, str], names: list[str]) -> bool | None:
    """A live page cites a Source of every named file, and each of those Sources lists that page back.

    None when a page that links both ways has a status the arm's registry cannot classify.
    """
    groups = [holding(facts, fixtures[name]) for name in names]
    linked = [
        page
        for page in facts["citing"]
        if all(
            any(
                row["key"] in page["sources"] and page["key"] in row["ingested_into"]
                for row in group
            )
            for group in groups
        )
    ]
    if any(page["live"] for page in linked):
        return True
    return None if any(page["live"] is None for page in linked) else False


SOURCE_CHECK_FUNCTIONS = {
    "source_saved": source_saved,
    "source_count": source_count,
    "current_source": current_source,
    "compiled_with_backlinks": compiled_with_backlinks,
}
UNKNOWN_REASONS = {
    "current_source": "the arm could not say whether a Source of the file was corrected",
    "compiled_with_backlinks": "the arm's status registry could not classify a page that cites the Sources",
}


def session_sources(facts: dict, before: list[str]) -> list[dict]:
    """The Sources saved in the session, as the judge sees them: how each is labelled, not where it lives."""
    saved = []
    for row in facts["sources"]:
        if row["path"] in before:
            continue
        frontmatter = row["frontmatter"]
        shown = {
            "title": frontmatter.get("title"),
            "source_kind": frontmatter.get("source_type"),
            "url": frontmatter.get("url"),
        }
        if frontmatter.get("document_form"):
            shown["document_form"] = frontmatter["document_form"]
        check = frontmatter.get("identity_check")
        if isinstance(check, dict) and check.get("state"):
            shown["identity_check_state"] = check["state"]
        saved.append(shown)
    return saved


def judge_context(spec: dict, saved: list[dict] | None) -> str:
    """What the judge reads besides the turn and the answer; the same view for every arm."""
    parts = []
    if spec.get("include_sources"):
        shown = json.dumps(saved, indent=1) if saved else "none"
        parts.append(f"Sources the assistant saved in this session, as stored:\n{shown}")
    if spec.get("reference"):
        parts.append(
            f"What the files really are (the assistant was not given this):\n{spec['reference']}"
        )
    return "".join(f"\n\n{part}" for part in parts)


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


def reads_sources(case: dict) -> bool:
    """Whether the case's checks need the vault's Sources, before and after the session."""
    rule = case["pass"]
    return any(name in rule for name in SOURCE_CHECKS) or bool(
        rule.get("judge", {}).get("include_sources")
    )


def play(case: dict, arm: Path, root: Path, *, executable: Path, model: str, max_turns: int):
    """Seed the arm's vault and guidance, then run one session. Returns (seeded note, finished process, record)."""
    work, block = root / "work", root / "custom-instructions.txt"
    plugin, vault = arm / "plugins" / "claude-code", fresh_vault(root, arm)
    work.mkdir(parents=True)
    http = case.get("transport", "stdio") == "http"
    record = {
        "instruction_block": lay_instruction_block(block, arm),
        "transport": "http" if http else "stdio",
        "fixtures": build_files(case.get("files", {}), work),
    }
    seeded = run_arm_side(
        arm, root, "seed", str(vault), str(work), stdin=json.dumps(case.get("seed"))
    )
    if case.get("seed_sources"):
        run_arm_side(
            arm, root, "seed_sources", str(vault), str(work), stdin=json.dumps(case["seed_sources"])
        )
    if reads_sources(case):
        # The Sources that exist before the session: seeded ones are not the session's to be judged on.
        before = run_arm_side(arm, root, "sources", str(vault))
        record["sources_before"] = [row["path"] for row in before["sources"]]
    tools = ["Read", "Skill"]
    allowed = ["mcp__exomem", "Skill", "Read(./**)", plugin_read_allow(plugin)]
    # The server's native MCP activation mode: the hooks route through the tools, not inject text.
    env = {**claude_env()[0], **hook_env(root), "EXOMEM_RETRIEVE_INJECT": "mcp"}
    if http:
        # What the documented handoff needs from a client: a digest, a descriptor file, and `exomem attach`.
        tools += ["Bash", "Write"]
        # An `Edit` rule covers the Write tool, and the descriptor goes into the work directory.
        allowed += ["Bash(exomem attach:*)", "Bash(sha256sum:*)", "Edit(./**)"]
        env["PATH"] = f"{lay_exomem_command(root, arm)}{os.pathsep}{env['PATH']}"
    actor = Arm(
        arm_id="incident",
        prominence=SHIPPED_PROMINENCE,
        tools=",".join(tools),
        uses_plugin=True,
        allowed_tools=",".join(allowed),
        uses_custom_instructions=True,
        configure_ref="membench.outcome.incidents",
    )

    def mint(session: dict) -> str:
        return run_arm_side(arm, root, "mint", stdin=json.dumps(session))["token"]

    with loopback_mcp(root, arm, mint) if http else nullcontext(mcp_server(root, arm)) as server:
        mcp = root / "mcp.json"
        mcp.write_text(json.dumps({"mcpServers": {"exomem": server}}))
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
        start = time.monotonic()
        done = subprocess.run(  # noqa: S603 - argv from the shared turn builder
            argv, cwd=work, env=env, capture_output=True, text=True, timeout=SESSION_TIMEOUT_SECONDS
        )
    record["seconds"] = round(time.monotonic() - start, 1)
    record["claude_md_sha256"] = hashlib.sha256((work / "CLAUDE.md").read_bytes()).hexdigest()
    (root / "stream.jsonl").write_text(done.stdout)
    return seeded, done, record


def judge_answer(
    case: dict, answer: str, root: Path, judge: Judge, saved: list[dict] | None = None
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
        JUDGE_PROMPT.format(turn=case["prompt"], answer=answer, extra=judge_context(spec, saved)),
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
    vault = str(root / "vault")
    # `mcp__exomem__` is the tool-name prefix that the "exomem" server name in the MCP config fixes.
    tools = [use.name.removeprefix("mcp__exomem__") for use in transcript.memory_tool_uses()]
    rule, checks, reasons, extra = case["pass"], {}, [], {}
    if seeded:
        after = run_arm_side(arm, root, "probe", vault, seeded["path"])
        extra["seed_after"] = {
            "exists": after["exists"],
            "lifecycle_class": after["lifecycle_class"],
        }
    if "seed_corrected" in rule:
        corrected = seed_corrected(seeded, after)
        checks["seed_corrected"] = (
            None if corrected is None else corrected == rule["seed_corrected"]
        )
        if corrected is None:
            reasons.append("the arm's status registry could not classify the seeded note")
    if "called_any" in rule:
        checks["called_any"] = bool(set(tools) & set(rule["called_any"]))
    facts = saved = None
    if "sources_before" in record:
        facts = run_arm_side(arm, root, "sources", vault)
        saved = session_sources(facts, record["sources_before"])
        extra["session_sources"] = saved
        for name in SOURCE_CHECKS:
            if name in rule:
                checks[name] = SOURCE_CHECK_FUNCTIONS[name](facts, record["fixtures"], rule[name])
                if checks[name] is None:
                    reasons.append(UNKNOWN_REASONS[name])
    if "judge" in rule:
        checks["judge"], reason = judge_answer(
            case, transcript.result_text.strip(), root, judge, saved
        )
        reasons += [reason] if reason else []
    invocations, firings = count_hook_activity(done.stdout)
    return (
        record
        | extra
        | {
            "passed": settle(checks),
            "reason": "; ".join(reasons) or None,
            "checks": checks,
            "tools": tools,
            "hook_invocations": invocations,
            "capture_nudge_firings": firings,
        }
    )


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
    # Refuses a credits declaration without a key before the run directory or any session exists.
    billing.require_billing_env(os.environ)
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
        # The billing source every session of this run must report (``billing.py``).
        "billing": billing.declared(),
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
