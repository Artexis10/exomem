"""Run Harvey LAB matters under four memory arms with subscription Claude sessions.

Each session is a fresh headless Claude Code process that writes one deliverable
for the task's instructions. What differs between arms is what the session can
use besides the converted documents:

- ``S``: nothing. Read, Write, Edit, Glob and Grep over the task folder.
- ``WP``: Exomem as its users get it, the primary Exomem arm, over a fresh disposable vault.
  A compile session first captures each document as a Source and writes the notes a later task
  would need, without seeing the task. A fresh task session then writes the deliverable. Both
  sessions get the shipped Claude plugin (skills and hooks), the documented balanced
  custom-instruction block, the Skill tool, the full Exomem tool surface and the file tools.
  The task prompt is the plain task and says nothing of memory.
- ``W``: ablation. Exomem over MCP alone: the compile session gets the full Exomem tools, and
  the task session gets Exomem's read tools and a sentence that its memory holds notes from
  this matter. No plugin, no instruction block, no Skill tool.
- ``WG``: ablation. The WP task session again on a copy of WP's vault and state taken right
  after the compile, with ``EXOMEM_DISABLE_GRAPH_INDEX=1``. A diagnostic of one product
  component, not a product configuration.

``judge`` scores the deliverables. ``membench outcome run lab`` drives this module.
"""

from __future__ import annotations

import json
import shutil
import traceback
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from functools import cached_property, partial
from pathlib import Path

from epistemic.journeys.f27_replay import (
    PLUGIN_DIR,
    AgentEnvelope,
    Arm,
    build_turn_argv,
    discover_agent_envelope,
    refuse_unsafe_out_dir,
)

from .. import record
from ..clbench.run import QuestionLog, single_turn
from ..faults import PARKED_DIR, LimitRejected, park, run_pool
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
from ..workspace import RUN_ROOT, require_pinned_checkout, sha256_of
from .prepare import CASES, UPSTREAM, slug

SESSION_TIMEOUT_SECONDS = 1800.0
#: What a second call under the same run id must repeat: the inputs of every matter it completes,
#: and the product and CLI that play it.
RESUME_KEYS = (
    "prepared",
    "tasks",
    "arms",
    "model_requested",
    "effort",
    "upstream_commit",
    "instruction_block",
    *record.IDENTITY_KEYS,
)


@dataclass(frozen=True)
class ArmSpec:
    exomem: bool = False
    #: Exomem as its users get it: the plugin, the balanced instruction block, the Skill tool and
    #: the full Exomem tool surface in every session, and a task prompt that says nothing of memory.
    shipped: bool = False
    #: A compile session fills the vault before the task session.
    compiles: bool = False
    #: A diagnostic ablation of one product component, not a product configuration.
    graph_off: bool = False


SPECS = {
    "S": ArmSpec(),
    "WP": ArmSpec(exomem=True, shipped=True, compiles=True),
    "W": ArmSpec(exomem=True, compiles=True),
    "WG": ArmSpec(exomem=True, shipped=True, graph_off=True),
}
ARMS = tuple(SPECS)
#: An arm that copies another arm's compiled vault instead of compiling its own.
COPIES = {"WG": "WP"}
FILE_TOOLS = "Read,Write,Edit,Glob,Grep"
# Path-scoped: an unscoped Glob or Grep allow would let one arm read another arm's files.
FILE_ALLOWS = ("Read(./**)", "Write(./**)", "Edit(./**)")
EXOMEM_READ_ALLOWS = tuple(
    f"mcp__exomem__{tool}" for tool in ("recall_context", "search_memory", "read_memory")
)
SHIPPED_ALLOWS = ("Skill", "mcp__exomem", PLUGIN_READ_ALLOW)

TASK_PROMPT = (
    "{instructions}\n\n"
    "The documents for this matter are in the `documents/` folder of your working directory, "
    "converted to Markdown text (one `.md` file per original document). "
    "Write the deliverable as Markdown to `output/{deliverable}.md`."
)
MEMORY_SENTENCE = "Your memory (the Exomem tools) holds notes compiled from this matter."
COMPILE_PROMPT = (
    "You are starting work on a legal matter. Its documents are in the `documents/` folder of "
    "your working directory, converted to Markdown text (one `.md` file per original document). "
    "A later task on this matter goes to a fresh session that has your Exomem memory and no "
    "access to this conversation.\n\n"
    "1. Read every document and capture each one as a Source in Exomem.\n"
    "2. Compile the knowledge that a later task in this matter would need into Exomem notes.\n\n"
    "You have no task yet. Stop when your notes are complete."
)


# --------------------------------------------------------------------------
# One session.
# --------------------------------------------------------------------------


class Lab:
    """What every session of one run shares: the CLI, its environment, the paths to protect."""

    def __init__(
        self,
        run_dir: Path,
        task_slugs: list[str],
        model: str,
        effort: str,
        envelope: AgentEnvelope | None = None,
    ) -> None:
        self.run_dir = run_dir
        self.task_slugs = task_slugs
        self.model = model
        self.effort = effort
        self.envelope = envelope or discover_agent_envelope()
        self.env, self.removed_env = claude_env()
        #: The instruction block of the shipped arms, laid once by ``run_lab``.
        self.block = run_dir / "custom-instructions.txt"

    @cached_property
    def product(self) -> dict:
        return record.product_record(self.run_dir)

    def protected(self, task_slug: str, arm: str) -> list[Path]:
        """Paths a session must not read: the rubric, every other run, and every other arm's files."""
        runs = RUN_ROOT / "runs"
        own = self.run_dir / task_slug / arm
        return [
            UPSTREAM,
            CASES.parent,
            *(p for p in runs.iterdir() if p.resolve() != self.run_dir),
            *(self.run_dir / s for s in self.task_slugs if s != task_slug),
            self.run_dir / PARKED_DIR,
            *(self.run_dir / task_slug / a for a in ARMS if a != arm),
            own / "vault",
            own / "exomem-state",
            own / "transcripts",
            own / "checkpoint",
        ]

    def argv(
        self, mcp_config: Path, allowed: list[str], protected: list[Path], *, shipped: bool = False
    ) -> list[str]:
        arm = Arm(
            arm_id="lab",
            prominence=SHIPPED_PROMINENCE if shipped else "instruction",
            tools=f"{FILE_TOOLS},Skill" if shipped else FILE_TOOLS,
            allowed_tools=",".join(allowed),
            uses_plugin=shipped,
            uses_custom_instructions=shipped,
            configure_ref="membench.outcome.lab",
        )
        argv = build_turn_argv(
            executable=self.envelope.executable,
            arm=arm,
            prompt=None,
            mcp_config=mcp_config,
            session_id=None,
            model=self.model,
            max_turns=None,
            first=True,
            append_system_prompt_file=self.block if shipped else None,
            plugin_dir=PLUGIN_DIR if shipped else None,
        )
        return argv + [
            "--effort",
            self.effort,
            "--disallowedTools",
            ",".join(f"Read(/{p}/**)" for p in protected),
        ]

    def env_for(self, arm_dir: Path, session: str, *, shipped: bool = False) -> dict[str, str]:
        """A session's environment. A shipped session's hooks keep their state in a home of its own under the arm."""
        return {**self.env, **hook_env(arm_dir, session)} if shipped else self.env

    def play(
        self,
        name: str,
        argv: list[str],
        env: dict[str, str],
        cwd: Path,
        transcript: Path,
        prompt: str,
        *,
        exomem: bool,
        reset: Callable[[int], None],
    ) -> dict:
        """One fresh session, one user turn. Timeouts and exits are recorded as faults, not raised."""
        refuse_unsafe_out_dir(cwd)
        transcript.parent.mkdir(exist_ok=True)
        log = QuestionLog(question_id=name, index=0)
        reply = single_turn(
            argv,
            env,
            cwd,
            transcript,
            prompt,
            log,
            model=self.model,
            exomem=exomem,
            timeout=SESSION_TIMEOUT_SECONDS,
            reset=reset,
        )
        # No reply means a timeout, or an exit that the session recorded as a harness fault.
        timed_out = reply is None and not log.harness_faults
        if timed_out:
            log.faults.append(f"timeout after {SESSION_TIMEOUT_SECONDS:.0f} s")
        print(
            f"  {name} s={log.seconds} tools={sum(log.tool_uses.values())} "
            f"faults={len(log.faults)} harness={len(log.harness_faults)}",
            flush=True,
        )
        return {
            "session": name,
            "reply": reply,
            "timed_out": timed_out,
            # A denied file or Exomem tool would cripple the arm without any other trace.
            "permission_denials": log.permission_denials,
            "log": log.__dict__,
        }


# --------------------------------------------------------------------------
# The arms.
# --------------------------------------------------------------------------


def lay_work_dir(prepared_task: Path, work: Path, *, output: bool) -> None:
    shutil.copytree(prepared_task / "documents", work / "documents")
    if output:
        (work / "output").mkdir()


def count_pages(vault: Path) -> int:
    return sum(1 for _ in vault.rglob("*.md"))


def deliverable_of(work: Path, name: str) -> tuple[str | None, list[str], str | None]:
    """``(text, output files, fault)``. A lone Markdown file under another name is taken, with a fault."""
    out = work / "output"
    files = sorted(p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file())
    exact = out / f"{name}.md"
    if exact.is_file():
        return exact.read_text(encoding="utf-8", errors="replace"), files, None
    markdown = [f for f in files if f.endswith(".md")]
    if len(markdown) == 1:
        text = (out / markdown[0]).read_text(encoding="utf-8", errors="replace")
        return text, files, f"deliverable written as output/{markdown[0]}, not output/{name}.md"
    return None, files, f"no output/{name}.md (output holds {files or 'nothing'})"


def run_arm(lab: Lab, case: dict, prepared_task: Path, arm: str, snapshot_to: str | None) -> dict:
    """One arm of one task. ``snapshot_to`` names the arm that gets a copy of this arm's compiled vault."""
    task_slug = slug(case["task_id"])
    arm_dir = lab.run_dir / task_slug / arm
    arm_dir.mkdir(
        parents=True, exist_ok=True
    )  # a copying arm's directory already holds the snapshot
    spec = SPECS[arm]
    if arm in COPIES and not (arm_dir / "exomem-state").exists():
        # Without the snapshot ``mcp_server`` would lay a fresh empty vault and the arm would measure nothing.
        fault = f"no snapshot of {COPIES[arm]}'s vault: {arm} not run"
        return write_result(lab, case, arm, {}, None, [], [fault], harness=[fault])
    task_prompt = TASK_PROMPT.format(
        instructions=case["instructions"], deliverable=case["deliverable"]
    )
    protected = lab.protected(task_slug, arm)
    sessions: dict[str, dict] = {}
    faults: list[str] = []
    #: Faults of the harness itself that no session recorded: the arm did not get the run it was meant to.
    harness: list[str] = []

    mcp_config = arm_dir / "mcp.json"
    if not spec.exomem:
        mcp_config.write_text(json.dumps({"mcpServers": {}}, indent=2))
        allowed = compile_allowed = list(FILE_ALLOWS)
    else:
        server = mcp_server(arm_dir)
        if spec.graph_off:
            server["env"]["EXOMEM_DISABLE_GRAPH_INDEX"] = "1"
        mcp_config.write_text(json.dumps({"mcpServers": {"exomem": server}}, indent=2))
        if spec.shipped:
            allowed = compile_allowed = [*FILE_ALLOWS, *SHIPPED_ALLOWS]
        else:
            allowed = [*FILE_ALLOWS, *EXOMEM_READ_ALLOWS]
            compile_allowed = [*FILE_ALLOWS, "mcp__exomem"]
            task_prompt += f"\n\n{MEMORY_SENTENCE}"

    def play(name: str, prompt: str, tools: list[str], work: Path, *, output: bool) -> dict:
        """One session of this arm. A retry starts from the folder, the vault and the hook state the session began with."""
        checkpoint = arm_dir / "checkpoint"
        if spec.exomem:
            copy_state(arm_dir, checkpoint)
        hook_home = hook_env(arm_dir, name)["EXOMEM_HOOK_HOME"]

        def reset(number: int) -> None:
            shutil.rmtree(work, ignore_errors=True)
            lay_work_dir(prepared_task, work, output=output)
            if number and spec.exomem:
                copy_state(checkpoint, arm_dir)
            if number and spec.shipped:
                shutil.rmtree(hook_home, ignore_errors=True)

        return lab.play(
            f"{task_slug}:{arm}:{name}",
            lab.argv(mcp_config, tools, protected, shipped=spec.shipped),
            lab.env_for(arm_dir, name, shipped=spec.shipped),
            work,
            arm_dir / "transcripts" / f"{name}.jsonl",
            prompt,
            exomem=spec.exomem,
            reset=reset,
        )

    if spec.compiles:
        # The compile session has its own folder: no task, no output folder, nothing to carry over.
        pages_before = count_pages(arm_dir / "vault")
        sessions["compile"] = play(
            "compile", COMPILE_PROMPT, compile_allowed, arm_dir / "compile-work", output=False
        )
        compiled = count_pages(arm_dir / "vault") - pages_before
        sessions["compile"]["pages_added"] = compiled
        if compiled == 0:
            faults.append("the compile session added no pages to the vault")
        if snapshot_to:
            try:
                copy_state(arm_dir, lab.run_dir / task_slug / snapshot_to)
            except Exception as exc:  # noqa: BLE001 - the copying arm is skipped and the fault stays visible
                harness.append(f"snapshot for {snapshot_to} failed: {exc}")

    work = arm_dir / "work"
    sessions["task"] = play("task", task_prompt, allowed, work, output=True)
    shutil.rmtree(arm_dir / "checkpoint", ignore_errors=True)
    text, files, fault = deliverable_of(work, case["deliverable"])
    if fault:
        faults.append(fault)
    if text is not None:
        (arm_dir / "deliverable.md").write_text(text, encoding="utf-8")
    # A session's harness faults are already among its faults; only the arm's own are added.
    faults += harness
    for session in sessions.values():
        faults += session["log"]["faults"]
        harness += session["log"]["harness_faults"]
    return write_result(lab, case, arm, sessions, text, files, faults, mcp_config, harness=harness)


def write_result(
    lab: Lab,
    case: dict,
    arm: str,
    sessions: dict,
    text: str | None,
    files: list[str],
    faults: list[str],
    mcp_config: Path | None = None,
    *,
    harness: Sequence[str] = (),
) -> dict:
    """Store one arm's result. ``faults`` is everything that went wrong; ``harness`` is the part that voids the unit."""
    first_init = next((s["log"]["init"] for s in sessions.values() if s["log"]["init"]), None)
    result = {
        "run_id": lab.run_dir.name,
        "task_id": case["task_id"],
        "arm": arm,
        "model_requested": lab.model,
        "effort": lab.effort,
        "actor": {
            "cli": lab.envelope.version,
            "product": lab.product,
            "mcp_config_sha256": sha256_of(mcp_config) if mcp_config else None,
            "observed": record.observed(first_init),
        },
        "deliverable": None if text is None else {"file": "deliverable.md", "chars": len(text)},
        "output_files": files,
        "sessions": sessions,
        "faults": faults,
        "harness_faults": list(harness),
    }
    arm_dir = lab.run_dir / slug(case["task_id"]) / arm
    arm_dir.mkdir(parents=True, exist_ok=True)
    (arm_dir / "result.json").write_text(json.dumps(result, indent=2))
    return result


def run_task(lab: Lab, prepared: Path, task_id: str, arms: list[str]) -> None:
    task_slug = slug(task_id)
    matter = lab.run_dir / task_slug
    if all((matter / arm / "result.json").exists() for arm in arms):
        print(f"{task_id}: already played", flush=True)
        return
    if matter.exists():
        # A copying arm's folder holds its source's snapshot, so a half-played matter cannot resume arm
        # by arm: it replays whole, and the old folder stays under ``unfinished/`` as evidence.
        park(lab.run_dir, matter)
    prepared_task = prepared / "tasks" / task_slug
    case = json.loads((prepared_task / "case.json").read_text())
    print(f"{task_id}: arms {','.join(arms)}", flush=True)
    for arm in arms:
        try:
            copy_to = next((c for c, source in COPIES.items() if source == arm and c in arms), None)
            run_arm(lab, case, prepared_task, arm, copy_to)
        except LimitRejected:
            raise  # not an arm's fault: the whole run stops, and this matter replays on the next call
        except Exception:  # noqa: BLE001 - one arm's failure must not stop the others
            error = traceback.format_exc()
            crash = f"arm crashed: {error.splitlines()[-1]}"
            write_result(lab, case, arm, {}, None, [], [crash], harness=[crash])
            (lab.run_dir / task_slug / arm / "fault.txt").write_text(error)
            print(f"{task_id} {arm}: FAULT {error.splitlines()[-1]}", flush=True)


# --------------------------------------------------------------------------
# The run.
# --------------------------------------------------------------------------


def run_lab(
    run_dir: Path,
    *,
    prepared: str,
    tasks: list[str] | None = None,
    arms: list[str],
    jobs: int = 1,
    model: str = ACTOR_MODEL,
    effort: str = ACTOR_EFFORT,
) -> Path:
    """Play ``arms`` over the tasks of a converted tree (``prepared`` is its run id)."""
    require_pinned_checkout("lab")
    refuse_unsafe_out_dir(run_dir)
    prepared_dir = RUN_ROOT / "runs" / prepared
    prepared_manifest = json.loads((prepared_dir / "manifest.json").read_text())
    task_ids = tasks or prepared_manifest["tasks"]
    unknown = set(task_ids) - set(prepared_manifest["tasks"])
    if unknown:
        raise SystemExit(f"not in {prepared}: {sorted(unknown)}")
    if not set(arms) <= set(ARMS):
        raise SystemExit(f"--arms takes {ARMS}")
    for copier, source in COPIES.items():
        if copier in arms and source not in arms:
            raise SystemExit(f"--arms: {copier} needs {source}, whose vault it copies")
    arms = [a for a in ARMS if a in arms]
    run_dir.mkdir(parents=True, exist_ok=True)
    lab = Lab(run_dir, [slug(t) for t in task_ids], model, effort)
    shipped_arms = [a for a in arms if SPECS[a].shipped]
    manifest = {
        "run_id": run_dir.name,
        "prepared": prepared,
        "upstream_commit": prepared_manifest["upstream_commit"],
        **record.checkout_state(),
        "product": lab.product,
        "cli": lab.envelope.version,
        "model_requested": model,
        "effort": effort,
        "session_timeout_seconds": SESSION_TIMEOUT_SECONDS,
        "env_removed": list(lab.removed_env),
        "tasks": task_ids,
        "arms": arms,
        "prompts": {
            "task": TASK_PROMPT,
            "memory_sentence": MEMORY_SENTENCE,
            "memory_sentence_arms": [a for a in arms if SPECS[a].exomem and not SPECS[a].shipped],
            "compile": COMPILE_PROMPT,
        },
        "file_tools": FILE_TOOLS,
        "allowed": {
            "file": list(FILE_ALLOWS),
            "exomem_read": list(EXOMEM_READ_ALLOWS),
            "shipped": list(SHIPPED_ALLOWS),
        },
        "shipped_arms": shipped_arms,
        "plugin_dir": str(PLUGIN_DIR) if shipped_arms else None,
        "instruction_block": lay_instruction_block(lab.block) if shipped_arms else None,
        # Isolation pins disable embeddings: the Exomem arms retrieve lexically.
        "exomem_retrieval": "lexical",
        "wg_ablation": "EXOMEM_DISABLE_GRAPH_INDEX=1",
        "wg_copy_of": COPIES["WG"],
    }
    manifest_file = run_dir / "manifest.json"
    if manifest_file.exists():
        # A second call completes the matters that a stop left unfinished, so it must be the same run.
        record.require_same_run(
            manifest_file, json.loads(manifest_file.read_text()), manifest, RESUME_KEYS
        )
    else:
        manifest_file.write_text(json.dumps(manifest, indent=2))
    run_pool(jobs, [partial(run_task, lab, prepared_dir, t, arms) for t in task_ids])
    return run_dir
