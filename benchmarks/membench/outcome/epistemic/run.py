"""Play f38 projects in lockstep across the arms ``N``, ``G``, ``F``, ``P`` and ``E``.

A project is a sequence of scripted work sessions. For session ``k`` every requested arm starts at
once, and session ``k + 1`` starts only after every arm has finished session ``k`` and its probes, so
no arm drifts in time from another. Each arm is a ``clbench`` arm without CL-Bench's per-task memory
sentence:

- ``N``: no memory and no history. It plays probes only: a session's main task cannot change what a later
  call sees, because the workspace resets before every call.
- ``G``: no memory, probes only, like ``N``. Its probe prompt holds the probe's contest set (``gold_prompt``).
- ``F``: a ``memory/`` folder in the workspace. The product's learning instruction, pointed at that
  folder, sits in the workspace ``CLAUDE.md``.
- ``P``: Exomem as its users get it: the shipped plugin, skills, hooks, instruction block and the
  learning instruction in the workspace ``CLAUDE.md``.
- ``E``: ablation. Exomem over MCP alone, with no instruction.

Before every call the workspace is replaced by the scripted files of the probe's session, so only the arm's
memory carries over. After an arm's main task, each of that session's probes runs as its own
``claude -p`` call from a snapshot of the arm's memory taken after the main task, and the snapshot is
restored after each probe: a probe never writes to the memory that a later call reads.

A harness fault that survives its retries invalidates the project's window for every arm, and the
project stops. A product failure (a timeout, a spent budget, a wrong answer) stays scored.
``membench outcome run epistemic`` drives this module.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import shutil
import threading
import traceback
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from functools import partial
from pathlib import Path

from epistemic.journeys.f27_replay import (
    AgentEnvelope,
    discover_agent_envelope,
    refuse_unsafe_out_dir,
)

from exomem.client_config import place_learning_instruction
from exomem.prominence import LEARNING_INSTRUCTION

from .. import record
from ..clbench.run import ARMS, ArmSpec, ClaudeActor, QuestionLog, single_turn
from ..faults import LimitRejected, park, run_pool
from ..isolation import copy_state, hook_env
from ..workspace import sha256_of
from . import layout
from .cases import GOLD_ARM, RESERVED_NAMES, refused

#: The suite's arms. ``G`` is ``N`` with a different probe prompt, so it is not one of CL-Bench's arms.
SPECS = {
    "N": ARMS["N"],
    GOLD_ARM: ArmSpec(GOLD_ARM, "none"),
    "F": ARMS["F"],
    "P": ARMS["P"],
    "E": ARMS["E"],
}
EPISTEMIC_ARMS = tuple(SPECS)
#: A main session does real work; a probe answers one question.
MAIN_TIMEOUT_SECONDS = 1200.0
PROBE_TIMEOUT_SECONDS = 600.0
#: What the judge sees of the files a probe changed is cut here, and the record says so.
MAX_DIFF_CHARS = 20_000
#: The arm's own folders: they are laid by the harness or hold its memory and its records of earlier answers,
#: so they never count as the actor's work and the actor may not read them.
PRIVATE_DIRS = (
    "vault",
    "exomem-state",
    "transcripts",
    "snapshot",
    "hook-home",
    "sessions",
    "probes",
)

#: F's instruction is the product's learning instruction with these explicit replacements (old, new).
#: ``folder_instruction`` fails when one misses, so a change to the product text cannot drift unseen.
FOLDER_REPLACEMENTS = (
    (
        "Use Exomem as the memory for my work.",
        "Use the folder `memory/` in your working directory as the memory for my work.",
    ),
    ("recall what Exomem holds about", "read what that folder holds about"),
    ("as one concise observation", "as one concise note in that folder"),
)

HISTORY_HEADER = (
    "Here is the record of the earlier work sessions on this project, oldest first. "
    "For each session it shows the task that session was given and the files it was given."
)
#: The header of arm G's prompt names no arm, store or memory product, and no task of an earlier session.
GOLD_HEADER = (
    "Here are documents from the earlier work on this project, oldest first. "
    "Each one shows the session it was given in and its file name."
)


def folder_instruction() -> str:
    """The learning instruction of arm ``F``: the product's text, pointed at the ``memory/`` folder."""
    text = LEARNING_INSTRUCTION
    for old, new in FOLDER_REPLACEMENTS:
        if text.count(old) != 1:
            raise RuntimeError(
                f"the product's learning instruction no longer holds {old!r} exactly once; "
                "update FOLDER_REPLACEMENTS in membench/outcome/epistemic/run.py"
            )
        text = text.replace(old, new)
    if "Exomem" in text:
        raise RuntimeError(
            "the folder instruction still names Exomem; update FOLDER_REPLACEMENTS in "
            "membench/outcome/epistemic/run.py"
        )
    return text


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def outside_paths(root: Path, arm_dir: Path) -> list[Path]:
    """Every path under ``root`` that is not on the way to ``arm_dir``: the case files, the upstreams,
    other runs, windows, projects and arms. An actor may read its own workspace and nothing else here."""
    paths: list[Path] = []
    node = arm_dir
    while node != root and root in node.parents:
        paths += [p for p in node.parent.iterdir() if p != node]
        node = node.parent
    return paths


class EpistemicActor(ClaudeActor):
    """A CL-Bench arm that plays a project: no per-task memory sentence, a workspace that resets, a memory that snapshots."""

    def __init__(
        self,
        spec,
        arm_dir: Path,
        model: str,
        effort: str,
        *,
        root: Path,
        envelope: AgentEnvelope | None = None,
    ) -> None:
        self.root = root
        super().__init__(spec, arm_dir, model, effort, "", envelope=envelope)
        # No arm gets a per-task memory sentence: an arm's instruction sits in its workspace CLAUDE.md.
        self.memory_instruction = None
        if spec.persists == "folder":
            self.learning_record = {
                "sha256": hashlib.sha256(folder_instruction().encode()).hexdigest(),
                "laid": "workspace CLAUDE.md",
                "derived": "the product's learning instruction, by FOLDER_REPLACEMENTS",
            }
        self.calls = 0

    def protected(self) -> list[Path]:
        """Read per call: a project, window or run that starts after this actor did sits under ``root`` too."""
        return [*outside_paths(self.root, self.arm_dir), *(self.arm_dir / n for n in PRIVATE_DIRS)]

    def _reset_work(self) -> None:
        self.lay_workspace({})

    def lay_workspace(self, files: dict[str, str]) -> None:
        """Replace the workspace by ``files``. Only the folder arm's ``memory/`` survives; each arm's instruction is laid again."""
        self.work.mkdir(parents=True, exist_ok=True)
        for entry in self.work.iterdir():
            if self.spec.persists == "folder" and entry.name == "memory":
                continue
            if entry.is_dir() and not entry.is_symlink():
                shutil.rmtree(entry)
            else:
                entry.unlink()
        for name, text in files.items():
            target = self.work / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
        if self.spec.persists == "folder":
            (self.work / "memory").mkdir(exist_ok=True)
            (self.work / "CLAUDE.md").write_text(folder_instruction() + "\n", encoding="utf-8")
        elif self.spec.shipped:
            place_learning_instruction(self.work / "CLAUDE.md", LEARNING_INSTRUCTION)

    def snapshot(self, target: Path) -> None:
        """Copy the arm's memory to ``target`` once no hook or server of the arm still runs."""
        if self.spec.exomem:
            copy_state(self.arm_dir, target)
        elif self.spec.persists == "folder":
            shutil.rmtree(target / "memory", ignore_errors=True)
            shutil.copytree(self.work / "memory", target / "memory")

    def restore(self, source: Path) -> None:
        """Put the arm's memory back to the snapshot in ``source``, once no hook or server of the arm still runs."""
        if self.spec.exomem:
            copy_state(source, self.arm_dir)
        elif self.spec.persists == "folder":
            shutil.rmtree(self.work / "memory", ignore_errors=True)
            shutil.copytree(source / "memory", self.work / "memory")


# --------------------------------------------------------------------------
# One call, and what it changed.
# --------------------------------------------------------------------------


def read_tree(work: Path) -> dict[str, str]:
    """The actor's files under ``work``, without the harness's own (memory, instruction, client folder)."""
    tree = {}
    for path in sorted(work.rglob("*")):
        relative = path.relative_to(work)
        if relative.parts[0] not in RESERVED_NAMES and path.is_file():
            tree[relative.as_posix()] = path.read_text(encoding="utf-8", errors="replace")
    return tree


def changed_files(before: dict[str, str], after: dict[str, str]) -> dict:
    """The files a call created, edited or deleted, as the judge reads them: a unified diff, cut at ``MAX_DIFF_CHARS``."""
    paths = sorted(p for p in before.keys() | after.keys() if before.get(p) != after.get(p))
    lines: list[str] = []
    for path in paths:
        old, new = before.get(path), after.get(path)
        lines += difflib.unified_diff(
            (old or "").splitlines(),
            (new or "").splitlines(),
            fromfile="/dev/null" if old is None else f"a/{path}",
            tofile="/dev/null" if new is None else f"b/{path}",
            lineterm="",
        )
    diff = "\n".join(lines)
    return {"files": paths, "diff": diff[:MAX_DIFF_CHARS], "truncated": len(diff) > MAX_DIFF_CHARS}


def play_call(
    actor: EpistemicActor,
    name: str,
    prompt: str,
    files: dict[str, str],
    *,
    timeout: float,
    snapshot: Path | None,
) -> dict:
    """One fresh ``claude -p`` call over ``files``. A harness fault is retried from the same inputs (``single_turn``).

    A retry puts the arm's memory back to ``snapshot`` and gives the call a fresh hook home, so it
    starts from what the first attempt started from.
    """
    actor.calls += 1
    index = actor.calls
    log = QuestionLog(question_id=name, index=index)
    transcript = actor.arm_dir / "transcripts" / f"{name}.jsonl"
    transcript.parent.mkdir(exist_ok=True)
    hook_home = hook_env(actor.arm_dir, f"q{index:03d}")["EXOMEM_HOOK_HOME"]

    def reset(number: int) -> None:
        if number:
            if snapshot is not None:
                actor.restore(snapshot)
            shutil.rmtree(hook_home, ignore_errors=True)
        actor.lay_workspace(files)

    reply = single_turn(
        actor.argv(),
        actor.session_env(index),
        actor.work,
        transcript,
        prompt,
        log,
        model=actor.model,
        exomem=actor.spec.exomem,
        timeout=timeout,
        reset=reset,
    )
    return {
        "name": name,
        "reply": reply,
        # No reply and no harness fault: the actor ran out of time. A product failure, scored.
        "timed_out": reply is None and not log.harness_faults,
        "log": log.__dict__,
    }


def probe_record(probe: dict, call: dict, changed: dict) -> dict:
    return {
        "probe_id": probe["probe_id"],
        "session": probe["session"],
        "reply": call["reply"],
        "timed_out": call["timed_out"],
        "changed": changed,
        "log": call["log"],
    }


def write_json(path: Path, body: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(body, indent=2))


# --------------------------------------------------------------------------
# One project, in lockstep.
# --------------------------------------------------------------------------


def run_arms(arms: list[str], work: Callable[[str], object]) -> dict[str, object]:
    """Run ``work(arm)`` for every arm at once and wait for all of them.

    A rejected limit is raised after every arm has stopped: it belongs to the account, and each arm
    meets the same rejection in its own session.
    """
    with ThreadPoolExecutor(max_workers=len(arms)) as pool:
        futures = {arm: pool.submit(work, arm) for arm in arms}
    results: dict[str, object] = {}
    limit: LimitRejected | None = None
    for arm, future in futures.items():
        try:
            results[arm] = future.result()
        except LimitRejected as stop:
            limit = limit or stop
    if limit:
        raise limit
    return results


def probe_prompt(arm: str, project: dict, probe: dict) -> str:
    """What ``arm`` is asked for ``probe``: the task alone, or for arm ``G`` the task after the contest set."""
    return gold_prompt(project, probe) if arm == GOLD_ARM else probe["task"]


def plays_sessions(spec: ArmSpec) -> bool:
    """Whether an arm plays each session's main task. An arm with no memory plays probes only: a main task
    carries nothing to a later call, since the workspace resets before every call, so it would cost a call
    and change nothing that a probe sees."""
    return spec.persists != "none"


def play_arm_session(
    actor: EpistemicActor,
    session: dict,
    probes: list[dict],
    snapshot: Path,
    stop: threading.Event,
    errors: list[dict],
    prompt_of: Callable[[dict], str],
) -> dict | None:
    """One arm's session: the main task, then each probe from the memory the main task left.

    An arm with no memory skips the main task (``plays_sessions``). Returns the first ``init`` event the
    arm's CLI sent. A harness fault that survived the retries is appended to ``errors`` and sets
    ``stop``: no arm starts another call. ``prompt_of`` gives the prompt of a probe.
    """
    arm, number = actor.spec.arm_id, session["session"]
    if stop.is_set():
        return None

    def fail(probe: str | None, faults: list[str]) -> None:
        errors.append({"arm": arm, "session": number, "probe": probe, "faults": faults})
        stop.set()

    init = None
    if plays_sessions(actor.spec):
        main = play_call(
            actor,
            f"s{number:02d}",
            session["task"],
            session["files"],
            timeout=MAIN_TIMEOUT_SECONDS,
            snapshot=snapshot,
        )
        write_json(actor.arm_dir / "sessions" / f"s{number:02d}.json", main)
        init = main["log"]["init"]
        if main["log"]["harness_faults"]:
            fail(None, main["log"]["harness_faults"])
            return init
        actor.snapshot(snapshot)
    for probe in probes:
        if stop.is_set():
            break
        files = {**session["files"], **probe["files"]}
        call = play_call(
            actor,
            f"s{number:02d}-{probe['probe_id']}",
            prompt_of(probe),
            files,
            timeout=PROBE_TIMEOUT_SECONDS,
            snapshot=snapshot,
        )
        init = init or call["log"]["init"]
        write_json(
            actor.arm_dir / "probes" / f"{probe['probe_id']}.json",
            probe_record(probe, call, changed_files(files, read_tree(actor.work))),
        )
        if call["log"]["harness_faults"]:
            fail(probe["probe_id"], call["log"]["harness_faults"])
            break
        # The probe's writes end here: the next call reads the memory the main task left.
        actor.restore(snapshot)
    return init


def arm_manifest(actor: EpistemicActor) -> dict:
    return {
        "arm": actor.spec.arm_id,
        "argv": actor.argv(),
        "mcp_config_sha256": sha256_of(actor.mcp_config),
        "env_removed": list(actor.removed_env),
        "memory_instruction": None,
        "instruction_block": actor.block_record,
        "learning_instruction": actor.learning_record,
    }


def play_project(
    name: str,
    project: dict,
    check: dict,
    *,
    root: Path,
    window_dir: Path,
    window: int,
    arms: list[str],
    model: str,
    effort: str,
    envelope: AgentEnvelope,
    on_init: Callable[[dict], None],
) -> dict:
    """Play every session of one project in lockstep; write ``state.json`` when the project ends.

    A rejected limit leaves no ``state.json``: the project then counts as unfinished and replays on
    the next call.
    """
    project_dir = window_dir / name
    for arm in arms:
        (project_dir / arm).mkdir(parents=True)
    started = now()
    actors = {
        arm: EpistemicActor(
            SPECS[arm],
            project_dir / arm,
            model,
            effort,
            root=root,
            envelope=envelope,
        )
        for arm in arms
    }
    snapshots = {arm: project_dir / arm / "snapshot" for arm in arms}
    for arm, actor in actors.items():
        write_json(project_dir / arm / "manifest.json", arm_manifest(actor))
        actor.snapshot(snapshots[arm])
    stop = threading.Event()
    errors: list[dict] = []

    def guarded(session: dict, probes: list[dict], arm: str) -> dict | None:
        try:
            return play_arm_session(
                actors[arm],
                session,
                probes,
                snapshots[arm],
                stop,
                errors,
                partial(probe_prompt, arm, project),
            )
        except LimitRejected:
            raise
        except Exception:  # noqa: BLE001 - one arm's crash invalidates the window and stays visible
            crash = traceback.format_exc()
            write_json(project_dir / arm / "crash.json", {"traceback": crash})
            errors.append(
                {"arm": arm, "session": session["session"], "crash": crash.splitlines()[-1]}
            )
            stop.set()
            return None

    played = 0
    for session in project["sessions"]:
        probes = [p for p in project["probes"] if p["session"] == session["session"]]
        inits = run_arms(arms, partial(guarded, session, probes))
        for init in inits.values():
            if init:
                on_init(init)
        if errors:
            break
        played += 1
        print(
            f"{name} window {window} session {session['session']} played by {','.join(arms)}",
            flush=True,
        )
    state = {
        "project": name,
        "window": window,
        "status": "invalid" if errors else "complete",
        "arms": arms,
        "started_at": started,
        "ended_at": now(),
        "sessions_played": played,
        "sessions_scheduled": len(project["sessions"]),
        "errors": errors,
        "case_check": check,
    }
    write_json(project_dir / "state.json", state)
    if errors:
        print(f"{name} window {window} INVALID: {errors}", flush=True)
    return state


# --------------------------------------------------------------------------
# One window of a run.
# --------------------------------------------------------------------------


class Window:
    """What one call shares: the window folder, its manifest and the CLI that plays it."""

    def __init__(
        self,
        run_dir: Path,
        root: Path,
        window: int,
        split: str,
        model: str,
        effort: str,
        envelope: AgentEnvelope | None,
    ) -> None:
        refuse_unsafe_out_dir(run_dir)
        self.root, self.number, self.model, self.effort = root, window, model, effort
        self.envelope = envelope or discover_agent_envelope()
        self.dir = run_dir / f"window-{window}"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.manifest_file = self.dir / "manifest.json"
        self._lock = threading.Lock()
        if not self.manifest_file.exists():
            write_json(
                self.manifest_file,
                {
                    "run_id": run_dir.name,
                    "window": window,
                    "split": split,
                    **record.checkout_state(),
                    "actor": record.actor_record(
                        cli=self.envelope.version,
                        model=model,
                        effort=effort,
                        root=self.dir / layout.PRODUCT_CHECK,
                    ),
                },
            )

    def note_init(self, init: dict) -> None:
        """Keep what the CLI reported of itself once: the first ``init`` event of the window."""
        with self._lock:
            manifest = json.loads(self.manifest_file.read_text())
            if "observed" not in manifest["actor"]:
                manifest["actor"]["observed"] = record.observed(init)
                write_json(self.manifest_file, manifest)


def play_window(
    window: Window,
    projects: dict[str, tuple[dict, dict]],
    arms: list[str],
    jobs: int,
) -> None:
    """Play ``projects`` in this window. A project that already ended is not replayed; one that stopped half way replays whole."""
    todo = []
    for name, (project, check) in projects.items():
        folder = window.dir / name
        if (folder / "state.json").exists():
            have = json.loads((folder / "state.json").read_text())["arms"]
            if have != arms:
                raise refused(
                    f"{name} was played in window {window.number} by arms {','.join(have)}; "
                    f"arms of a window play together, so use another --window"
                )
            print(f"{name} window {window.number}: already played", flush=True)
            continue
        if folder.exists():
            # Memory carries across sessions, so a half-played project cannot resume: it replays whole.
            park(window.dir, folder)
        todo.append(
            partial(
                play_project,
                name,
                project,
                check,
                root=window.root,
                window_dir=window.dir,
                window=window.number,
                arms=arms,
                model=window.model,
                effort=window.effort,
                envelope=window.envelope,
                on_init=window.note_init,
            )
        )
    run_pool(jobs, todo)


# --------------------------------------------------------------------------
# The solvable gate: arm N, with the whole history in the prompt, probes only.
# --------------------------------------------------------------------------


def file_blocks(files: dict[str, str]) -> list[str]:
    """Scripted files as a prompt shows them, so that the two reference prompts differ only in what they select."""
    return [f"--- file: {name} ---\n{text}" for name, text in files.items()]


def history_prompt(project: dict, probe: dict) -> str:
    """The scripted task and files of sessions 1 to the probe's, then the probe's task, under a neutral header."""
    parts = [HISTORY_HEADER]
    for session in project["sessions"]:
        if session["session"] > probe["session"]:
            break
        parts.append(f"=== Session {session['session']} ===\nTask: {session['task']}")
        parts += file_blocks(session["files"])
    parts.append(f"=== Your task now ===\n{probe['task']}")
    return "\n\n".join(parts)


def contest_files(project: dict, probe: dict) -> list[tuple[int, dict[str, str]]]:
    """The files of the probe's contest set: ``(session, files)`` in session order, each session's files in its own order.

    Nothing else of an earlier session is here: not its task and not another of its files.
    """
    wanted = {(ref["session"], ref["file"]) for ref in probe["key"]["contest"]}
    found = []
    for session in project["sessions"]:
        held = {n: t for n, t in session["files"].items() if (session["session"], n) in wanted}
        if held:
            found.append((session["session"], held))
    return found


def gold_prompt(project: dict, probe: dict) -> str:
    """Arm ``G``'s prompt: the probe's contest set in session order under a neutral header, then the probe's task.

    A probe with an empty contest set gets its task alone, as arm ``N`` does: a line that said nothing
    bears on the question would tell ``G`` more than the contest set holds.
    """
    held = contest_files(project, probe)
    if not held:
        return probe["task"]
    parts = [GOLD_HEADER]
    for number, files in held:
        parts.append(f"=== Session {number} ===")
        parts += file_blocks(files)
    parts.append(f"=== Your task now ===\n{probe['task']}")
    return "\n\n".join(parts)


def play_history_probe(window: Window, name: str, project: dict, probe: dict, folder: Path) -> None:
    """Arm ``N`` answers one probe with the full history in its prompt. Each probe has a folder of its own."""
    session = next(s for s in project["sessions"] if s["session"] == probe["session"])
    files = {**session["files"], **probe["files"]}
    folder.mkdir(parents=True)
    actor = EpistemicActor(
        ARMS["N"],
        folder,
        window.model,
        window.effort,
        root=window.root,
        envelope=window.envelope,
    )
    call = play_call(
        actor,
        probe["probe_id"],
        history_prompt(project, probe),
        files,
        timeout=PROBE_TIMEOUT_SECONDS,
        snapshot=None,
    )
    if call["log"]["init"]:
        window.note_init(call["log"]["init"])
    write_json(
        folder / "probe.json",
        probe_record(probe, call, changed_files(files, read_tree(actor.work))),
    )
    print(f"{name} {probe['probe_id']} history probe played", flush=True)


def play_history(window: Window, projects: dict[str, tuple[dict, dict]], jobs: int) -> None:
    """Every probe of ``projects``, one at a time per job. A probe with a result and no harness fault is not replayed."""
    todo = []
    for name, (project, check) in projects.items():
        base = window.dir / "history" / name
        write_json(base / "case-check.json", check)
        for probe in project["probes"]:
            folder = base / probe["probe_id"]
            if (folder / "probe.json").exists():
                if not json.loads((folder / "probe.json").read_text())["log"]["harness_faults"]:
                    continue
            if folder.exists():
                park(window.dir, folder)
            todo.append(partial(play_history_probe, window, name, project, probe, folder))
    run_pool(jobs, todo)
