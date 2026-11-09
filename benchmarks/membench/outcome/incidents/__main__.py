"""Replay product incidents on real models, one fresh session per run, and compare code versions.

Each file under ``cases/`` holds one incident's cases. A case seeds one note through Exomem's own
writer, sends one user turn to Claude Code in print mode, and checks the outcome:

- ``seed_body_changed``: the seeded note's body differs after the session (the agent corrected it);
- ``called_any``: the session called at least one of the named Exomem tools;
- ``judge``: a blind model fills the case's JSON schema from the turn and the answer, and every
  ``require`` field must match.

An arm is a checkout: its ``src`` runs the MCP server and its ``plugins/claude-code`` supplies the
skill and hooks, in native MCP activation mode. Every session gets its own vault, state and hook
home, and the arms run interleaved in one window.

    PYTHONPATH=benchmarks:src python -m membench.outcome.incidents \\
        --arm base=../exomem-base --arm changed=. --repeats 3 --run-root /var/tmp/incidents-run
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from exomem.client_config import place_learning_instruction
from exomem.prominence import LEARNING_INSTRUCTION

from ..isolation import claude_env, lay_instruction_block, server_pins

CASES_DIR = Path(__file__).parent / "cases"
SESSION_TIMEOUT_SECONDS = 900
JUDGE_TIMEOUT_SECONDS = 300
#: The checks this runner implements; a case's `pass` names a subset.
CHECKS = ("seed_body_changed", "called_any", "judge")

SEED = """
import json, sys
from pathlib import Path
from exomem import commands
from exomem.init import init_vault
from exomem.governance.principal import owner_principal, request_scope
vault, seed = Path(sys.argv[1]), json.load(sys.stdin)
init_vault(vault)
with request_scope(owner_principal(surface="mcp")):
    note = commands.op_note(vault, title=seed["title"], note_type=seed["note_type"], content=seed["content"])
print(note["path"])
"""


def load_cases(selected: list[str] | None) -> list[dict]:
    cases = []
    for path in sorted(CASES_DIR.glob("*.json")):
        incident = json.loads(path.read_text(encoding="utf-8"))
        for case in incident["cases"]:
            cases.append({**case, "incident_file": path.name})
    for case in cases:
        # A misspelled rule would leave a case with no checks, and it would pass every run.
        unknown = set(case["pass"]) - set(CHECKS)
        if unknown or not case["pass"]:
            raise SystemExit(f"case {case['id']}: pass rules must be among {CHECKS}, got {sorted(case['pass'])}")
    if selected:
        cases = [case for case in cases if case["id"] in selected]
    return cases


def note_body(path: Path) -> str | None:
    """The note's body without frontmatter, or None when the file is gone."""
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8")
    if text.startswith("---\n"):
        end = text.find("\n---\n", 4)
        return text[end + 5 :] if end >= 0 else text
    return text


def child_env(pins: dict[str, str]) -> dict[str, str]:
    base = {k: v for k, v in os.environ.items() if not k.startswith(("EXOMEM_", "KB_", "VIRTUAL_ENV"))}
    return {**base, **pins}


def actor_env(root: Path, pins: dict[str, str]) -> dict[str, str]:
    env, _ = claude_env()
    env = {k: v for k, v in env.items() if not k.startswith("VIRTUAL_ENV")}
    env.update({k: v for k, v in pins.items() if k.startswith("EXOMEM_")})
    env["EXOMEM_HOOK_HOME"] = str(root / "hook-home")
    env["EXOMEM_RETRIEVE_INJECT"] = "mcp"
    return env


def seed_vault(root: Path, pins: dict[str, str], seed: dict) -> Path:
    vault = root / "vault"
    done = subprocess.run(  # noqa: S603 - fixed argv
        [sys.executable, "-c", SEED, str(vault)], input=json.dumps(seed),
        env=child_env(pins), check=True, capture_output=True, text=True,
    )
    relative = done.stdout.strip().splitlines()[-1]
    for candidate in (vault / relative, vault / "Knowledge Base" / relative):
        if candidate.exists():
            return candidate
    raise SystemExit(f"seeded note not found: {relative}")


def read_stream(stdout: str) -> tuple[list[str], str, float | None]:
    """(tool names in call order, final answer, notional cost) from a stream-json transcript."""
    tools, answer, cost = [], "", None
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "assistant":
            for part in event.get("message", {}).get("content", []):
                if part.get("type") == "tool_use":
                    tools.append(part["name"].removeprefix("mcp__exomem__"))
        elif event.get("type") == "result":
            answer, cost = event.get("result") or "", event.get("total_cost_usd")
    return tools, answer, cost


def judge(case: dict, answer: str, root: Path, model: str) -> dict:
    """The blind verdict: the judge sees the user turn and the answer, never the arm."""
    spec = case["pass"]["judge"]
    prompt = (
        "You grade one assistant answer. Read the user's turn and the answer, then fill the schema.\n\n"
        f"User turn: {case['prompt']}\n\nAnswer:\n<<<\n{answer}\n>>>"
    )
    env, _ = claude_env()
    done = subprocess.run(  # noqa: S603 - fixed argv
        ["claude", "-p", prompt, "--model", model, "--output-format", "json",
         "--json-schema", json.dumps(spec["schema"]), "--tools", "", "--strict-mcp-config",
         "--setting-sources", "project", "--no-session-persistence"],
        cwd=root, env={k: v for k, v in env.items() if not k.startswith("VIRTUAL_ENV")},
        capture_output=True, text=True, timeout=JUDGE_TIMEOUT_SECONDS,
    )
    try:
        return json.loads(done.stdout)["structured_output"]
    except (json.JSONDecodeError, KeyError, TypeError):
        return {"judge_error": (done.stderr or done.stdout)[-300:]}


def run_session(case: dict, arm: Path, root: Path, model: str) -> dict:
    root.mkdir(parents=True)
    pins = server_pins(root)
    pins["PYTHONPATH"] = str(arm / "src")
    note = seed_vault(root, pins, case["seed"])
    before = note_body(note)
    mcp = root / "mcp.json"
    mcp.write_text(json.dumps({"mcpServers": {"exomem": {
        "command": sys.executable, "args": ["-m", "exomem", "--transport", "stdio"], "env": pins,
    }}}))
    work = root / "work"
    work.mkdir()
    place_learning_instruction(work / "CLAUDE.md", LEARNING_INSTRUCTION)
    block = root / "custom-instructions.txt"
    lay_instruction_block(block)
    argv = [
        "claude", "-p", case["prompt"],
        "--strict-mcp-config", "--mcp-config", str(mcp), "--setting-sources", "project",
        "--output-format", "stream-json", "--verbose", "--include-hook-events",
        "--allowedTools", "mcp__exomem,Skill,Read(./**)", "--tools", "Read,Skill",
        "--model", model, "--no-session-persistence",
        "--plugin-dir", str(arm / "plugins" / "claude-code"),
        "--append-system-prompt-file", str(block),
    ]
    start = time.monotonic()
    done = subprocess.run(  # noqa: S603 - fixed argv
        argv, cwd=work, env=actor_env(root, pins), capture_output=True, text=True,
        timeout=SESSION_TIMEOUT_SECONDS,
    )
    (root / "stream.jsonl").write_text(done.stdout)
    tools, answer, cost = read_stream(done.stdout)
    (root / "answer.txt").write_text(answer)
    checks = {}
    rule = case["pass"]
    if "seed_body_changed" in rule:
        checks["seed_body_changed"] = (note_body(note) != before) == rule["seed_body_changed"]
    if "called_any" in rule:
        checks["called_any"] = bool(set(tools) & set(rule["called_any"]))
    if "judge" in rule:
        verdict = judge(case, answer, root, model)
        checks["judge"] = all(verdict.get(k) == v for k, v in rule["judge"]["require"].items())
        (root / "verdict.json").write_text(json.dumps(verdict, indent=2))
    return {
        "exit": done.returncode, "seconds": round(time.monotonic() - start, 1),
        "tools": tools, "notional_usd": cost, "checks": checks,
        "passed": done.returncode == 0 and all(checks.values()),
    }


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m membench.outcome.incidents")
    parser.add_argument("--arm", action="append", required=True, help="NAME=CHECKOUT, repeatable")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--case", action="append", help="case id to run; default all")
    parser.add_argument("--model", default="opus")
    parser.add_argument("--run-root", type=Path, required=True)
    args = parser.parse_args(argv)
    arms = dict(item.split("=", 1) for item in args.arm)
    arms = {name: Path(path).resolve() for name, path in arms.items()}
    results = args.run_root / "results.jsonl"
    args.run_root.mkdir(parents=True, exist_ok=True)
    tally: dict[tuple[str, str], list[bool]] = {}
    for rep in range(1, args.repeats + 1):
        for case in load_cases(args.case):
            order = list(arms) if rep % 2 else list(reversed(arms))
            for name in order:
                root = args.run_root / case["id"] / name / f"r{rep}"
                try:
                    record = run_session(case, arms[name], root, args.model)
                except Exception as exc:  # noqa: BLE001 - one failed session is a recorded failure, not a lost run
                    record = {"error": repr(exc)[:400], "passed": False}
                record.update(case=case["id"], arm=name, rep=rep)
                with results.open("a") as handle:
                    handle.write(json.dumps(record) + "\n")
                tally.setdefault((case["id"], name), []).append(record["passed"])
                print(json.dumps(record), flush=True)
    for (case_id, name), passes in sorted(tally.items()):
        print(f"{case_id}\t{name}\t{sum(passes)}/{len(passes)} passed")


if __name__ == "__main__":
    main()
