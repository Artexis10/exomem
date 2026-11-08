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
- ``PP``: the primary Exomem arm. Exactly ``P``, and the task prompt also carries CL-Bench's
  per-task memory sentence pointed at Exomem: the sentence ``E`` gets, in the place ``F`` has its own.
- ``P``: Exomem as its users get it. The shipped Claude plugin (skills and hooks) at its default
  prominence, the documented balanced custom-instruction block, the learning instruction that
  ``exomem setup`` places in the user's ``CLAUDE.md``, the Skill tool and the full Exomem tool
  surface over a fresh disposable vault. The harness reads project settings only, so the learning
  instruction is laid in the workspace ``CLAUDE.md``, which Claude Code delivers through the same
  channel as the user-level file. The task prompt carries no memory instruction. It measures the
  product gap that a capture change must close, and only describes. The workspace resets per
  question, so Exomem is the only thing that persists. Each turn records the characters the hooks
  injected.
- ``E``: ablation. Exomem over MCP alone on a fresh disposable vault, with our memory
  instruction in the task prompt. The workspace resets per question.
- ``EH``: diagnostic. ``E`` plus the shipped plugin at its default activation; each turn
  records the characters the hooks injected.

Every arm has the same built-in file tools, scoped to its workspace. The
``oracle`` and ``wrong`` actors play no model: they prove the grader before any
model run (R0), through ``run_cell``.

``membench outcome run clbench`` drives this module.
"""

from __future__ import annotations

import hashlib
import json
import os
import queue
import shutil
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from epistemic.journeys.f27_replay import (
    PLUGIN_DIR,
    AgentEnvelope,
    Arm,
    build_turn_argv,
    discover_agent_envelope,
    refuse_unsafe_out_dir,
)

from exomem.client_config import place_learning_instruction
from exomem.prominence import LEARNING_INSTRUCTION
from membench.utility.switch_eval.session import hook_push, rejected_limit

from .. import record
from ..faults import LimitRejected, init_faults, with_retries
from ..isolation import (
    ACTOR_EFFORT,
    ACTOR_MODEL,
    PLUGIN_READ_ALLOW,
    SHIPPED_PROMINENCE,
    claude_env,
    copy_state,
    hook_env,
    lay_instruction_block,
    mcp_server,
)
from ..workspace import pin, require_pinned_checkout, sha256_of

UPSTREAM = pin("clbench").checkout()
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
    plugin: bool = False  # the shipped Claude plugin (skills and hooks) loads beside the memory
    #: Exomem as its users get it: the balanced instruction block and the Skill tool, and the task
    #: prompt carries no memory instruction unless ``told``. Needs ``plugin``.
    shipped: bool = False
    #: The task prompt carries the per-task memory sentence although the arm ships the plugin.
    told: bool = False

    @property
    def exomem(self) -> bool:
        return self.persists == "exomem"


ARMS = {
    "N": ArmSpec("N", "none"),
    "F": ArmSpec("F", "folder"),
    "PP": ArmSpec("PP", "exomem", plugin=True, shipped=True, told=True),
    "P": ArmSpec("P", "exomem", plugin=True, shipped=True),
    "E": ArmSpec("E", "exomem"),
    "EH": ArmSpec("EH", "exomem", plugin=True),
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
    #: Everything that went wrong in the session, product failures and harness faults alike.
    faults: list[str] = field(default_factory=list)
    #: The subset that is evidence the harness failed to run the product; it voids the unit (see ``faults``).
    harness_faults: list[str] = field(default_factory=list)
    #: One entry per attempt that was retried: ``{"attempt": n, "faults": [...]}``.
    retries: list[dict] = field(default_factory=list)
    init: dict | None = None
    #: One entry per user turn sent: hook event name -> characters that hook injected.
    hook_chars: list[dict[str, int]] = field(default_factory=list)
    #: Tools the CLI refused for want of permission. A denied tool cripples an arm without another trace.
    permission_denials: list[str] = field(default_factory=list)
    #: ``result`` events the CLI sent. A session with none reports no usage, which the reports mark as partial.
    results: int = 0

    def harness(self, fault: str) -> None:
        """Record evidence of a harness fault; it also shows in ``faults``."""
        if fault not in self.harness_faults:
            self.harness_faults.append(fault)
            self.faults.append(fault)


def injected_chars(event: dict) -> int:
    """Characters one ``hook_response`` event added to the session's context."""
    text = hook_push(event)
    if not text:
        # A Stop hook injects through its block ``reason``, not through ``additionalContext``.
        for raw in (event.get("stdout"), event.get("output")):
            try:
                payload = json.loads(raw) if isinstance(raw, str) else None
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict) and isinstance(payload.get("reason"), str):
                text = payload["reason"]
                break
    return len(text)


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

    def __init__(
        self,
        argv: list[str],
        env: dict[str, str],
        cwd: Path,
        transcript: Path,
        *,
        model: str,
        exomem: bool,
    ) -> None:
        """``model`` and ``exomem`` say what every ``init`` event must show (see ``faults.init_faults``)."""
        self._model, self._exomem = model, exomem
        self._path = transcript
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
        try:
            return self._exchange(text, log, timeout)
        except LimitRejected:
            # The run stops here, so this process must not outlive it.
            self.close()
            raise
        finally:
            # The pump flushes every line, so a transcript that is still empty here is lost.
            if not self._path.exists() or self._path.stat().st_size == 0:
                log.harness(f"transcript {self._path.name} is missing or empty")

    def _exchange(self, text: str, log: QuestionLog, timeout: float) -> str | None:
        assert self._proc.stdin
        message = {"type": "user", "message": {"role": "user", "content": text}}
        self._proc.stdin.write(json.dumps(message) + "\n")
        self._proc.stdin.flush()
        deadline = time.monotonic() + timeout
        injected: dict[str, int] = {}
        log.hook_chars.append(injected)
        while True:
            remaining = deadline - time.monotonic()
            try:
                if remaining <= 0:
                    raise queue.Empty
                line = self._lines.get(timeout=remaining)
            except queue.Empty:
                # A timeout is a scored loss, unless the CLI never started its session at all.
                if log.init is None:
                    log.harness("the CLI sent no init event before the timeout")
                return None
            if line is None:
                log.harness(f"the CLI exited with {self._proc.poll()} before it answered")
                return None
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if (rejected := rejected_limit(event)) is not None:
                raise LimitRejected(rejected)
            kind = event.get("type")
            if kind == "system" and event.get("subtype") == "init":
                # Every user turn brings an init event; each one is checked, the first is kept.
                for fault in init_faults(event, model=self._model, exomem=self._exomem):
                    log.harness(fault)
                if log.init is None:
                    log.init = {
                        key: event.get(key)
                        for key in (
                            "model",
                            "tools",
                            "mcp_servers",
                            "skills",
                            "plugins",
                            "permissionMode",
                            "apiKeySource",
                            "cwd",
                        )
                    }
            elif kind == "system" and event.get("subtype") == "hook_response":
                name = str(event.get("hook_event"))
                injected[name] = injected.get(name, 0) + injected_chars(event)
            elif kind == "assistant":
                for block in event.get("message", {}).get("content", []):
                    if block.get("type") == "tool_use":
                        name = block.get("name", "?")
                        log.tool_uses[name] = log.tool_uses.get(name, 0) + 1
            elif kind == "result":
                log.results += 1
                for key, value in (event.get("usage") or {}).items():
                    if isinstance(value, int):
                        log.usage[key] = log.usage.get(key, 0) + value
                log.notional_cost_usd += float(event.get("total_cost_usd") or 0.0)
                log.permission_denials += [
                    str(d.get("tool_name", "?")) for d in event.get("permission_denials") or []
                ]
                if event.get("is_error"):
                    # A scored loss, not a harness fault (PREREGISTRATION section 7): the product ended in
                    # an error, such as a spent turn or context budget. The result text is the error, not
                    # an answer, so the reply is empty.
                    log.faults.append(
                        f"result error: {event.get('subtype')}: {event.get('result')}"
                    )
                    return ""
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


def single_turn(
    argv: list[str],
    env: dict[str, str],
    cwd: Path,
    transcript: Path,
    prompt: str,
    log: QuestionLog,
    *,
    model: str,
    exomem: bool,
    timeout: float,
    reset: Callable[[int], None],
) -> str | None:
    """One fresh session and one user turn, replayed with the same inputs on a harness fault (at most twice).

    ``reset(n)`` puts back what attempt ``n`` starts from. A retry keeps its own transcript, and the
    session's usage adds up over every attempt: the log shows what the unit cost. A timeout is a
    scored loss and is not retried. Returns the reply, or None when there was none.
    """
    attempts = 0
    replies: list[str | None] = []

    def before(number: int) -> None:
        log.harness_faults.clear()
        log.init = None
        log.hook_chars.clear()
        reset(number)

    def attempt() -> dict:
        nonlocal attempts
        path = transcript if not attempts else transcript.with_suffix(f".retry{attempts}.jsonl")
        attempts += 1
        session = ClaudeSession(argv, env, cwd, path, model=model, exomem=exomem)
        start = time.monotonic()
        try:
            replies.append(session.send(prompt, log, timeout))
        finally:
            log.seconds = round(log.seconds + time.monotonic() - start, 1)
            session.close()
        return {}

    _, log.retries = with_retries(attempt, lambda _result: list(log.harness_faults), before)
    log.turns = 1
    return replies[-1]


class ClaudeActor:
    """The model under test, behind one memory arm."""

    def __init__(
        self,
        spec: ArmSpec,
        arm_dir: Path,
        model: str,
        effort: str,
        memory_instruction: str,
        *,
        envelope: AgentEnvelope | None = None,
    ) -> None:
        self.spec = spec
        self.arm_dir = arm_dir
        self.work = arm_dir / "work"
        self.model = model
        self.effort = effort
        self.envelope = envelope or discover_agent_envelope()
        # The shipped arm's task prompt says nothing about memory (the plugin and the block do), unless told.
        self.memory_instruction = (
            None
            if spec.shipped and not spec.told
            else EXOMEM_MEMORY_INSTRUCTION
            if spec.exomem
            else memory_instruction.format(memory_dir=str(self.work / "memory"))
        )
        self.mcp_config = arm_dir / "mcp.json"
        servers = {"exomem": mcp_server(arm_dir)} if spec.exomem else {}
        self.mcp_config.write_text(json.dumps({"mcpServers": servers}, indent=2))
        self.block = arm_dir / "custom-instructions.txt"
        self.block_record = lay_instruction_block(self.block) if spec.shipped else None
        # The learning instruction `exomem setup` places: laid by `_reset_work` with setup's own writer.
        self.learning_record = (
            {
                "sha256": hashlib.sha256(LEARNING_INSTRUCTION.encode()).hexdigest(),
                "laid": "workspace CLAUDE.md",
            }
            if spec.shipped
            else None
        )
        # Path-scoped: an unscoped Glob or Grep allow would let one arm read another arm's memory.
        allowed = ["Read(./**)", "Write(./**)", "Edit(./**)"]
        if spec.plugin:
            allowed.append(PLUGIN_READ_ALLOW)
        if spec.shipped:
            allowed.append("Skill")
        if spec.exomem:
            allowed.append("mcp__exomem")
        self.arm = Arm(
            arm_id=spec.arm_id,
            prominence=SHIPPED_PROMINENCE if spec.shipped else "instruction",
            tools=f"{FILE_TOOLS},Skill" if spec.shipped else FILE_TOOLS,
            allowed_tools=",".join(allowed),
            uses_plugin=spec.plugin,
            uses_custom_instructions=spec.shipped,
            configure_ref="membench.outcome.clbench",
        )
        self.env, self.removed_env = claude_env()
        self._reset_work()

    def session_env(self, index: int) -> dict[str, str]:
        """The environment of question ``index``'s session.

        The hooks are child processes of the CLI: their state and every Exomem path stay in the
        arm. Each question stands for a separate user session spread over time, and the hook's
        cooldowns are wall-clock values tuned for interactive use, so each question gets a fresh
        hook home and the cooldown stamps of the one before cannot silence its nudge.
        """
        if not self.spec.plugin:
            return self.env
        return {**self.env, **hook_env(self.arm_dir, f"q{index:03d}")}

    def _reset_work(self) -> None:
        """An empty workspace; the folder arm's memory folder starts empty.

        A shipped arm's workspace holds the learning instruction as setup writes it.
        """
        shutil.rmtree(self.work, ignore_errors=True)
        self.work.mkdir(parents=True)
        if self.spec.persists == "folder":
            (self.work / "memory").mkdir()
        if self.spec.shipped:
            place_learning_instruction(self.work / "CLAUDE.md", LEARNING_INSTRUCTION)

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
            append_system_prompt_file=self.block if self.spec.shipped else None,
            plugin_dir=PLUGIN_DIR if self.spec.plugin else None,
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
        """Upstream's Claude-system prompt shape: feedback, question, schema, and the memory instruction when the arm has one."""
        parts = []
        if pending:
            parts.append(f"FEEDBACK FROM PREVIOUS ACTION: {pending}\n")
        parts.append(turn["prompt"])
        parts.append(turn["schema_instruction"])
        if self.memory_instruction:
            parts.append("\n\n" + self.memory_instruction)
        return "\n".join(parts)

    def _checkpoint(self) -> None:
        """Keep what the question starts from, so a retry has the same inputs."""
        checkpoint = self.arm_dir / "checkpoint"
        if self.spec.exomem:
            copy_state(self.arm_dir, checkpoint)
        elif self.spec.persists == "folder":
            # The folder arm's whole workspace carries from question to question, not only memory/.
            shutil.rmtree(checkpoint, ignore_errors=True)
            shutil.copytree(self.work, checkpoint / "work")

    def _restore(self, index: int) -> None:
        """Put the arm back to the checkpoint: its memory and workspace, and a fresh hook home for the question."""
        checkpoint = self.arm_dir / "checkpoint"
        if self.spec.exomem:
            copy_state(checkpoint, self.arm_dir)
        if self.spec.persists == "folder":
            shutil.rmtree(self.work, ignore_errors=True)
            shutil.copytree(checkpoint / "work", self.work)
        else:
            self._reset_work()
        if self.spec.plugin:
            hook_home = hook_env(self.arm_dir, f"q{index:03d}")["EXOMEM_HOOK_HOME"]
            shutil.rmtree(hook_home, ignore_errors=True)

    def _first_turn(
        self, log: QuestionLog, text: str, transcript: Path
    ) -> tuple[ClaudeSession, str | None]:
        """Start the question's session and send its first turn, retrying a harness fault with the same inputs.

        Only the first turn can be replayed: upstream's task cannot rewind, so a fault after the
        question's first step stands and voids the run.
        """
        if self.spec.persists != "none":
            self._checkpoint()
        sessions: list[ClaudeSession] = []

        def reset(number: int) -> None:
            if sessions:
                sessions.pop().close()
            log.harness_faults.clear()
            log.init = None
            log.hook_chars.clear()
            if number:
                self._restore(log.index)
            elif self.spec.persists != "folder":
                self._reset_work()

        def attempt() -> dict:
            session = ClaudeSession(
                self.argv(),
                self.session_env(log.index),
                self.work,
                transcript,
                model=self.model,
                exomem=self.spec.exomem,
            )
            sessions.append(session)
            return {"reply": session.send(text, log, TURN_TIMEOUT_SECONDS)}

        result, log.retries = with_retries(attempt, lambda _result: list(log.harness_faults), reset)
        return sessions[-1], result["reply"]

    def play_question(self, env: Env, turn: dict, log: QuestionLog, pending: str | None) -> dict:
        transcript = self.arm_dir / "transcripts" / f"q{log.index:03d}-{log.question_id}.jsonl"
        transcript.parent.mkdir(exist_ok=True)
        start = time.monotonic()
        text = self.prompt(pending, turn)
        turn_start = time.monotonic()
        session, reply = self._first_turn(log, text, transcript)
        try:
            while True:
                log.turns += 1
                if reply is None:
                    # A timeout is a scored loss; a crash is a harness fault and voids the unit, but the
                    # question still ends through upstream's own fallback so the task stays consistent.
                    if not log.harness_faults:
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
                text = self.prompt(pending, turn)
                turn_start = time.monotonic()
                reply = session.send(text, log, TURN_TIMEOUT_SECONDS)
        finally:
            log.seconds = round(time.monotonic() - start, 1)
            session.close()


# --------------------------------------------------------------------------
# The run.
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class CellSpec:
    """One arm played once over one question sequence."""

    run_dir: Path
    arm: str  # an ARMS key, or "oracle" / "wrong" for the grader proofs
    variant: str | None = None
    schedule: str | None = None
    seed: int | None = None
    run_index: int | None = None
    limit: int | None = None
    model: str = ACTOR_MODEL
    effort: str = ACTOR_EFFORT

    @property
    def dir(self) -> Path:
        suffix = "" if self.run_index is None else f"-r{self.run_index}"
        return self.run_dir / f"{self.arm}{suffix}"


def run_cell(cell: CellSpec) -> Path:
    require_pinned_checkout("clbench")
    refuse_unsafe_out_dir(cell.run_dir)
    arm_dir = cell.dir
    if arm_dir.exists():
        raise SystemExit(f"{arm_dir} exists; choose a new --run-id")
    arm_dir.mkdir(parents=True)
    env = Env(arm_dir / "env-server.log")
    try:
        opened = env.call(
            "open",
            task="database_exploration",
            variant=cell.variant,
            schedule=cell.schedule,
            seed=cell.seed,
            run_index=cell.run_index,
            num_instances=cell.limit,
        )
        if cell.arm in ARMS:
            actor = ClaudeActor(
                ARMS[cell.arm],
                arm_dir,
                cell.model,
                cell.effort,
                opened["file_memory_instruction"],
            )
        else:
            actor = KeyActor(wrong=cell.arm == "wrong")
        manifest = {
            "run_id": cell.run_dir.name,
            "arm": cell.arm,
            "task": {
                "name": "database_exploration",
                "variant": cell.variant,
                "schedule": cell.schedule,
                "seed": cell.seed,
                "run_index": cell.run_index,
                "instances": opened["instances"],
            },
            "upstream_commit": pin("clbench").commit,
            **record.checkout_state(),
        }
        if isinstance(actor, ClaudeActor):
            manifest["actor"] = {
                **record.actor_record(
                    cli=actor.envelope.version, model=cell.model, effort=cell.effort, root=arm_dir
                ),
                "mcp_config_sha256": sha256_of(actor.mcp_config),
                "argv": actor.argv(),
                "env_removed": list(actor.removed_env),
                "memory_instruction": actor.memory_instruction,
                "retrieval": "lexical" if actor.spec.exomem else None,
                "plugin": {
                    "plugin_dir": str(PLUGIN_DIR),
                    "activation": "shipped default: no EXOMEM_RETRIEVE_INJECT override",
                    "hook_home": str(arm_dir / "hook-home"),
                    "hook_home_scope": "a fresh directory per question, named q<NNN>",
                }
                if actor.spec.plugin
                else None,
                "instruction_block": actor.block_record,
                "learning_instruction": actor.learning_record,
            }
        (arm_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))

        turn, pending, index = opened["turn"], None, 0
        first_init: dict | None = None
        voided: dict | None = None
        outcomes = (arm_dir / "outcomes.jsonl").open("a")
        while True:
            log = QuestionLog(question_id=str(turn["instance_id"]), index=index)
            result = actor.play_question(env, turn, log, pending)
            first_init = first_init or log.init
            outcomes.write(json.dumps({"outcome": result["outcome"], "log": log.__dict__}) + "\n")
            outcomes.flush()
            print(
                f"{arm_dir.name} q{index:03d} {log.question_id} "
                f"reward={result['outcome'] and result['outcome']['reward']} "
                f"turns={log.turns} s={log.seconds} faults={len(log.faults)}",
                flush=True,
            )
            if log.harness_faults:
                # Memory carries across a run's questions, so a surviving harness fault voids the whole
                # run for every arm: nothing after it is worth playing.
                voided = {
                    "question_id": log.question_id,
                    "index": index,
                    "faults": log.harness_faults,
                }
                break
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
        if isinstance(actor, ClaudeActor):
            manifest["actor"]["observed"] = record.observed(first_init)
            (arm_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))
        # An unfinished cell has no upstream score; the marker keeps the cell from being replayed.
        evaluation = {"voided": voided} if voided else env.call("evaluate")["result"]
        (arm_dir / "evaluation.json").write_text(json.dumps(evaluation, indent=2))
        print(
            f"{arm_dir.name} {'VOIDED' if voided else 'score=' + str(evaluation.get('score'))}",
            flush=True,
        )
    finally:
        env.close()
    return arm_dir
