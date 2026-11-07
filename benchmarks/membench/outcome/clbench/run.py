"""Run CL-Bench ``database_exploration`` with one actor and one memory arm.

The upstream task scores every answer; this driver only plays the agent side.
Each question starts a fresh headless Claude Code session on the operator's
subscription, as in the published memory experiments. Within a question the
session continues across QUERY steps. What persists between questions is the
arm:

- ``N``: nothing. The workspace and the pending feedback reset per question,
  which is upstream's stateless baseline.
- ``F``: a plain memory folder in the workspace, with upstream's own Claude
  memory instruction.
- ``E``: Exomem over MCP on a fresh disposable vault. The workspace resets per
  question, so Exomem is the only thing that persists.

Every arm has the same built-in file tools, scoped to its workspace. The
``oracle`` and ``wrong`` actors play no model: they prove the grader before any
model run (R0).

Usage (from the repository root, with ``PYTHONPATH=benchmarks:src``)::

    python -m membench.outcome.clbench.run --run-id r0-oracle --actor oracle
    python -m membench.outcome.clbench.run --run-id r1-dev --actor claude --arm F
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from epistemic.journeys.f27_replay import (
    Arm,
    build_turn_argv,
    discover_agent_envelope,
    environment_floor,
    refuse_unsafe_out_dir,
)

from ..isolation import REPO_ROOT, mcp_server

RUN_ROOT = Path(os.environ.get("OUTCOME_EVAL_ROOT", "/var/tmp/exomem-outcome-evals"))
UPSTREAM = RUN_ROOT / "upstream" / "clbench"
ENV_SERVER = Path(__file__).with_name("env_server.py")

#: Upstream's Claude system gives each CLI invocation 300 s; the task's own 60 s
#: value is declared but never enforced by upstream's runner.
TURN_TIMEOUT_SECONDS = 300.0

#: The Exomem arm's instruction, worded in parallel with upstream's file-memory one.
EXOMEM_MEMORY_INSTRUCTION = (
    "You should learn and store information from each interaction, so use your memory. "
    "You have a persistent memory system in the Exomem tools (MCP server `exomem`). "
    "Recall from it at the start of each turn and save new information to it as you "
    "learn things that will be useful in future turns."
)

FILE_TOOLS = "Read,Write,Edit,Glob,Grep"


@dataclass(frozen=True)
class ArmSpec:
    arm_id: str
    persists: str  # "none" | "folder" | "exomem"

    @property
    def exomem(self) -> bool:
        return self.persists == "exomem"


ARMS = {
    "N": ArmSpec("N", "none"),
    "F": ArmSpec("F", "folder"),
    "E": ArmSpec("E", "exomem"),
}


class Env:
    """The upstream task, in its own interpreter."""

    def __init__(self, log: Path) -> None:
        python = UPSTREAM / ".venv" / "bin" / "python"
        self._log = log.open("a")
        self._proc = subprocess.Popen(  # noqa: S603 - fixed argv
            [str(python), "-I", str(ENV_SERVER)],
            cwd=UPSTREAM,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self._log,
            text=True,
        )

    def call(self, op: str, **kwargs) -> dict:
        assert self._proc.stdin and self._proc.stdout
        self._proc.stdin.write(json.dumps({"op": op, **kwargs}) + "\n")
        self._proc.stdin.flush()
        line = self._proc.stdout.readline()
        if not line:
            raise RuntimeError(f"env server exited during {op}")
        reply = json.loads(line)
        if "fault" in reply:
            raise RuntimeError(f"env server fault during {op}: {reply['fault']}")
        return reply

    def close(self) -> None:
        if self._proc.stdin:
            self._proc.stdin.close()
        self._proc.wait(timeout=30)
        self._log.close()


# --------------------------------------------------------------------------
# Actors.
# --------------------------------------------------------------------------


@dataclass
class QuestionLog:
    question_id: str
    index: int
    turns: int = 0
    repairs: int = 0
    timeouts: int = 0
    seconds: float = 0.0
    tool_uses: dict[str, int] = field(default_factory=dict)
    usage: dict[str, int] = field(default_factory=dict)
    notional_cost_usd: float = 0.0
    faults: list[str] = field(default_factory=list)
    init: dict | None = None


class KeyActor:
    """Answers from upstream's key without exploring: ``oracle`` must score 1, ``wrong`` 0."""

    def __init__(self, wrong: bool) -> None:
        self._wrong = wrong
        self._answers: dict[str, str] = {}
        for name in ("questions.json", "questions_post_drift.json"):
            for q in json.loads((UPSTREAM / "data" / "database_exploration" / name).read_text()):
                self._answers[str(q["question_id"])] = str(q["answer"])

    def play_question(self, env: Env, turn: dict, log: QuestionLog, pending: str | None) -> dict:
        answer = "no answer" if self._wrong else self._answers[str(turn["instance_id"])]
        log.turns = 1
        return env.call("step", action={"action": "ANSWER", "content": answer})


class ClaudeSession:
    """One headless Claude Code process, fed user turns as stream-json."""

    def __init__(self, argv: list[str], env: dict[str, str], cwd: Path, transcript: Path) -> None:
        self._transcript = transcript.open("a")
        self._proc = subprocess.Popen(  # noqa: S603 - argv from the shared f27 builder
            argv,
            cwd=cwd,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
        self._lines: queue.Queue[str | None] = queue.Queue()
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self) -> None:
        assert self._proc.stdout
        for line in self._proc.stdout:
            self._transcript.write(line)
            self._transcript.flush()
            self._lines.put(line)
        self._lines.put(None)

    def send(self, text: str, log: QuestionLog, timeout: float) -> str | None:
        """Send one user turn and return the final reply text, or None on timeout or exit."""
        assert self._proc.stdin
        message = {"type": "user", "message": {"role": "user", "content": text}}
        self._proc.stdin.write(json.dumps(message) + "\n")
        self._proc.stdin.flush()
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            try:
                line = self._lines.get(timeout=remaining)
            except queue.Empty:
                return None
            if line is None:
                log.faults.append(f"actor exited with {self._proc.poll()}")
                return None
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            kind = event.get("type")
            if kind == "system" and event.get("subtype") == "init" and log.init is None:
                log.init = {
                    key: event.get(key)
                    for key in (
                        "model",
                        "tools",
                        "mcp_servers",
                        "permissionMode",
                        "apiKeySource",
                        "cwd",
                    )
                }
            elif kind == "assistant":
                for block in event.get("message", {}).get("content", []):
                    if block.get("type") == "tool_use":
                        name = block.get("name", "?")
                        log.tool_uses[name] = log.tool_uses.get(name, 0) + 1
            elif kind == "result":
                for key, value in (event.get("usage") or {}).items():
                    if isinstance(value, int):
                        log.usage[key] = log.usage.get(key, 0) + value
                log.notional_cost_usd += float(event.get("total_cost_usd") or 0.0)
                if event.get("is_error"):
                    log.faults.append(
                        f"result error: {event.get('subtype')}: {event.get('result')}"
                    )
                return event.get("result") or ""

    def close(self) -> None:
        if self._proc.stdin and not self._proc.stdin.closed:
            self._proc.stdin.close()
        try:
            self._proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            os.killpg(self._proc.pid, 9)
            self._proc.wait()
        self._transcript.close()


class ClaudeActor:
    """The model under test, behind one memory arm."""

    def __init__(
        self, spec: ArmSpec, arm_dir: Path, model: str, effort: str, memory_instruction: str
    ) -> None:
        self.spec = spec
        self.arm_dir = arm_dir
        self.work = arm_dir / "work"
        self.model = model
        self.effort = effort
        self.envelope = discover_agent_envelope()
        self.memory_instruction = (
            EXOMEM_MEMORY_INSTRUCTION
            if spec.exomem
            else memory_instruction.format(memory_dir=str(self.work / "memory"))
        )
        self.mcp_config = arm_dir / "mcp.json"
        servers = {"exomem": mcp_server(arm_dir)} if spec.exomem else {}
        self.mcp_config.write_text(json.dumps({"mcpServers": servers}, indent=2))
        # Path-scoped: an unscoped Glob or Grep allow would let one arm read another arm's memory.
        allowed = ["Read(./**)", "Write(./**)", "Edit(./**)"]
        if spec.exomem:
            allowed.append("mcp__exomem")
        self.arm = Arm(
            arm_id=spec.arm_id,
            prominence="instruction",
            tools=FILE_TOOLS,
            allowed_tools=",".join(allowed),
            uses_plugin=False,
            uses_custom_instructions=False,
            configure_ref="membench.outcome.clbench",
        )
        floor, self.removed_env = environment_floor(os.environ)
        self.env = {k: v for k, v in floor.items() if not k.startswith(("EXOMEM_", "KB_"))}
        # Claude Code's own auto memory would be a second, unmeasured store in every arm.
        self.env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] = "1"
        self._reset_work()

    def _reset_work(self) -> None:
        if self.work.exists():
            shutil.rmtree(self.work)
        (self.work / "memory").mkdir(
            parents=True
        ) if self.spec.persists == "folder" else self.work.mkdir(parents=True)

    def argv(self) -> list[str]:
        argv = build_turn_argv(
            executable=self.envelope.executable,
            arm=self.arm,
            prompt=None,
            mcp_config=self.mcp_config,
            session_id=None,
            model=self.model,
            max_turns=None,
            first=True,
            append_system_prompt_file=None,
            plugin_dir=None,
        )
        protected = [UPSTREAM, self.arm_dir / "vault", self.arm_dir / "exomem-state"]
        argv += [
            "--effort",
            self.effort,
            "--disallowedTools",
            ",".join(f"Read(/{p}/**)" for p in protected),
        ]
        return argv

    def prompt(self, pending: str | None, turn: dict) -> str:
        """Upstream's Claude-system prompt shape: feedback, question, schema, memory instruction."""
        parts = []
        if pending:
            parts.append(f"FEEDBACK FROM PREVIOUS ACTION: {pending}\n")
        parts.append(turn["prompt"])
        parts.append(turn["schema_instruction"])
        parts.append("\n\n" + self.memory_instruction)
        return "\n".join(parts)

    def play_question(self, env: Env, turn: dict, log: QuestionLog, pending: str | None) -> dict:
        if self.spec.persists != "folder":
            self._reset_work()
        transcript = self.arm_dir / "transcripts" / f"q{log.index:03d}-{log.question_id}.jsonl"
        transcript.parent.mkdir(exist_ok=True)
        session = ClaudeSession(self.argv(), self.env, self.work, transcript)
        start = time.monotonic()
        try:
            while True:
                text = self.prompt(pending, turn)
                turn_start = time.monotonic()
                reply = session.send(text, log, TURN_TIMEOUT_SECONDS)
                log.turns += 1
                if reply is None:
                    log.timeouts += 1
                    result = env.call(
                        "timeout",
                        timeout_seconds=TURN_TIMEOUT_SECONDS,
                        elapsed_seconds=time.monotonic() - turn_start,
                    )
                else:
                    result = env.call("respond", text=reply)
                    if not result["valid"]:
                        # Upstream's Claude system repairs once in the same conversation.
                        log.repairs += 1
                        repair = "\n".join(
                            [
                                "Your previous response did not match the required benchmark schema.",
                                f"Validation error: {result['error']}",
                                "Previous response:",
                                reply,
                                "Return ONLY a corrected JSON object for the same task.",
                                "Original prompt:",
                                text,
                            ]
                        )
                        reply = session.send(repair, log, TURN_TIMEOUT_SECONDS)
                        log.turns += 1
                        result = env.call("respond", text=reply or "")
                        if not result["valid"]:
                            log.faults.append(
                                "schema: no valid action after one repair (scored as an empty answer)"
                            )
                            result = env.call("step", action={"action": "ANSWER", "content": ""})
                pending = result["observation"]
                if result["instance_complete"] or result["done"]:
                    return result
                turn = result["turn"]
        finally:
            log.seconds = round(time.monotonic() - start, 1)
            session.close()


# --------------------------------------------------------------------------
# The run.
# --------------------------------------------------------------------------


def run(args: argparse.Namespace) -> Path:
    run_dir = refuse_unsafe_out_dir(RUN_ROOT / "runs" / args.run_id)
    arm_id = args.arm if args.actor == "claude" else args.actor
    arm_dir = run_dir / arm_id
    if arm_dir.exists():
        raise SystemExit(f"{arm_dir} exists; choose a new --run-id")
    arm_dir.mkdir(parents=True)
    env = Env(arm_dir / "env-server.log")
    try:
        opened = env.call(
            "open",
            task="database_exploration",
            variant=args.variant,
            schedule=args.schedule,
            seed=args.seed,
            run_index=args.run_index,
            num_instances=args.limit,
        )
        if args.actor == "claude":
            actor = ClaudeActor(
                ARMS[args.arm], arm_dir, args.model, args.effort, opened["file_memory_instruction"]
            )
        else:
            actor = KeyActor(wrong=args.actor == "wrong")
        manifest = {
            "run_id": args.run_id,
            "arm": arm_id,
            "task": {
                "name": "database_exploration",
                "variant": args.variant,
                "schedule": args.schedule,
                "seed": args.seed,
                "run_index": args.run_index,
                "instances": opened["instances"],
            },
            "upstream_commit": subprocess.run(
                ["git", "-C", str(UPSTREAM), "rev-parse", "HEAD"], capture_output=True, text=True
            ).stdout.strip(),
            "product_revision": subprocess.run(
                ["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"], capture_output=True, text=True
            ).stdout.strip(),
        }
        if isinstance(actor, ClaudeActor):
            manifest["actor"] = {
                "cli": actor.envelope.version,
                "model_requested": args.model,
                "effort": args.effort,
                "argv": actor.argv(),
                "env_removed": list(actor.removed_env),
                "memory_instruction": actor.memory_instruction,
                "retrieval": "lexical" if actor.spec.exomem else None,
            }
        (arm_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))

        turn, pending, index = opened["turn"], None, 0
        outcomes = (arm_dir / "outcomes.jsonl").open("a")
        while True:
            log = QuestionLog(question_id=str(turn["instance_id"]), index=index)
            result = actor.play_question(env, turn, log, pending)
            outcomes.write(json.dumps({"outcome": result["outcome"], "log": log.__dict__}) + "\n")
            outcomes.flush()
            print(
                f"q{index:03d} {log.question_id} reward={result['outcome'] and result['outcome']['reward']} "
                f"turns={log.turns} s={log.seconds} faults={len(log.faults)}",
                flush=True,
            )
            if result["done"]:
                break
            # N is upstream's stateless baseline: no feedback crosses a question boundary.
            pending = (
                None
                if (isinstance(actor, ClaudeActor) and actor.spec.persists == "none")
                else result["observation"]
            )
            turn, index = result["turn"], index + 1
        outcomes.close()
        evaluation = env.call("evaluate")["result"]
        (arm_dir / "evaluation.json").write_text(json.dumps(evaluation, indent=2))
        print(f"score={evaluation.get('score')}", flush=True)
    finally:
        env.close()
    return arm_dir


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--actor", choices=("claude", "oracle", "wrong"), default="claude")
    parser.add_argument("--arm", choices=sorted(ARMS), default="F")
    parser.add_argument("--variant", default=None)
    parser.add_argument("--schedule", default=None)
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--run-index", type=int, default=None)
    parser.add_argument(
        "--limit", type=int, default=None, help="Run only the first N instances (smoke runs)."
    )
    parser.add_argument("--model", default="claude-sonnet-5-5")
    parser.add_argument("--effort", default="high")
    args = parser.parse_args(argv)
    if args.variant is None and args.schedule is None and args.limit is None:
        # A variant fixes its question count, so a --limit smoke runs the task's plain defaults.
        args.variant = "multi_group"
    run(args)


if __name__ == "__main__":
    main()
