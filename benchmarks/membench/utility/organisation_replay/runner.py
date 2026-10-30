"""Bind an arm, then run each situation: seed, capture, readback, fresh recall."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from epistemic.journeys.f27_replay import AgentEnvelope, Arm, build_turn_argv, refuse_unsafe_out_dir

from membench.utility.switch_eval import protocol
from membench.utility.switch_eval.arms import ArmSpec, sha256_text
from membench.utility.switch_eval.session import (
    ABORT,
    ACTOR_FAILURE,
    API_KEY_VARIABLES,
    HARNESS_FAULT,
    OK,
    QUOTA,
    ClaudeSessionExecutor,
    PlannedSession,
    RunAborted,
    RunPaused,
    SessionInputs,
    load_session,
    run_session,
    session_env,
)
from membench.utility.switch_eval.state import (
    SERVER_NAME,
    Layout,
    Slot,
    StateError,
    _child_env,
    capture_surface,
    mcp_server_entry,
    tree_digest,
)

from . import seed as seed_module
from .runfile import read_jsonl, session_object
from .scripted import DoorFault, ScriptedSession
from .situation import Situation

#: The f34 call guard: at most 30 Exomem calls per user turn, equal in both arms.
CALL_GUARD = 30
#: f34's fresh session follows the shipped default packet workflow, maps.
PACKET_MODE = "maps"
#: The product's client surface for the actor's CLI, which both client shapes
#: present (``prominence.detect_surface``); prominence resolves for it.
ACTOR_SURFACE = "claude-code"
#: A slot path placeholder, so a config digest names the config, not where a run sits.
_SLOT = "<slot>"
#: The 40-hex Git revision format the binding record requires.
_REVISION = re.compile(r"[0-9a-f]{40}")
#: Retries for a seed fault: the protocol's harness-fault retry count.
SEED_RETRIES = protocol.HARNESS_FAULT_RETRIES
#: The checkout this package runs from; its repository holds both revisions.
_CHECKOUT = Path(__file__).resolve().parents[4]
#: Whether each arm's writer records edge-review reasons. The comparison fixes two
#: roles, and only the candidate's writer records a reason for a kept generic edge.
REASON_CAPABLE = {"baseline": False, "candidate": True}
#: A situation's checkpoint until ``run.json`` is written; its run status is ``PAUSED``.
PROGRESS_FILE = "progress.json"
#: The run status of a checkpoint: not an outcome, so never in a ``run.json``.
PAUSED = "paused"
#: A measured session's readback window, kept beside its attempts.
WINDOW_FILE = "window.json"
#: The code files the shipped hook install deploys: Python hooks and their shell
#: wrappers (the product's ``_hooks`` directory holds only these two kinds).
HOOK_CODE_SUFFIXES = (".py", ".sh")


@dataclass(frozen=True)
class RunConfig:
    out: Path
    arm: str
    profile: str
    model: str
    client: AgentEnvelope
    launcher: tuple[str, ...]
    revision: str
    base_revision: str
    wheel: Path | None
    standin: bool = False
    #: The arm's role in the comparison; only the bind reads it.
    role: str | None = None


def refuse_api_keys(env: Mapping[str, str]) -> None:
    present = [name for name in API_KEY_VARIABLES if env.get(name)]
    if present:
        raise SystemExit(
            f"refused: {', '.join(present)} is set; the actor runs on the subscription"
        )


def arm_spec(
    config: RunConfig,
    shape: str,
    tools: tuple[str, ...],
    wired_hooks: frozenset[str] = frozenset(),
) -> ArmSpec:
    # The hooked shape wires the shipped default hook set (``shipped_hooks``),
    # not the switch-evaluation push, so it has no push mode: the session check
    # accepts exactly the events that install wired. Hookless wires none.
    return ArmSpec(
        arm_id=config.arm,
        packet_mode=PACKET_MODE,
        hook_mode=None,
        tools=tools,
        instruction_source=(),
        profile=config.profile,
        wired_hooks=wired_hooks if shape == "hooked" else frozenset(),
    )


def _portable(text: str, slot: Slot) -> str:
    return text.replace(str(slot.root), _SLOT)


def shipped_hooks(slot: Slot, spec: ArmSpec, settings: Path) -> frozenset[str]:
    """Run the shipped ``exomem install-hook`` with its defaults, into the slot.

    No activation mode, so the Claude Code hook set a default install wires:
    capture, retrieve and continuation. Only the two locations move into the
    slot; the hooks keep their state in the slot's hook home (the session
    environment's ``EXOMEM_HOOK_HOME``). Returns the hook events it wired.
    """

    done = subprocess.run(  # noqa: S603 - this interpreter, the product's own installer
        [
            sys.executable,
            "-m",
            "exomem",
            "install-hook",
            "--hook-dir",
            str(slot.hook_home / "hooks"),
            "--settings",
            str(settings),
            "--json",
        ],
        env=_child_env(os.environ, slot, spec),
        cwd=slot.cwd,
        capture_output=True,
        text=True,
    )
    if done.returncode != 0:
        raise StateError(f"install-hook failed: {done.stderr[-1500:]}")
    report = json.loads(done.stdout)
    if report.get("activation_mode") != "legacy" or not report.get("wired"):
        raise StateError(f"install-hook did not wire the default set: {done.stdout[-600:]}")
    return frozenset(item["event"] for item in report["installed"])


def session_files(
    slot: Slot, spec: ArmSpec, files_dir: Path, *, hooked: bool
) -> tuple[dict[str, Path], frozenset[str]]:
    """The MCP config and hook settings a measured session reads, and the hook events wired."""

    files_dir.mkdir(parents=True, exist_ok=True)
    entry = mcp_server_entry(
        slot,
        spec,
        log=slot.runtime / "tap.jsonl",
        server_stderr=slot.runtime / "server.stderr",
        # The tap's ready gate probes through the due-state carrier and would
        # settle the prior agent's promotion; the settle session warmed the index.
        skip_ready_gate=True,
        budget=CALL_GUARD,
    )
    paths = {"mcp.json": files_dir / "mcp.json"}
    paths["mcp.json"].write_text(
        json.dumps({"mcpServers": {SERVER_NAME: entry}}, indent=2, sort_keys=True), encoding="utf-8"
    )
    if not hooked:
        return paths, frozenset()
    paths["settings.json"] = files_dir / "settings.json"
    return paths, shipped_hooks(slot, spec, paths["settings.json"])


def hook_scripts_digest(slot: Slot) -> str:
    """Digest of the hook code the shipped install deployed into the slot, by file name.

    The hooks import their sibling helpers, so every deployed code file counts,
    not only the scripts the settings name. A hook's own state files beside
    them are data and are left out.
    """

    hook_dir = slot.hook_home / "hooks"
    digest = hashlib.sha256()
    for path in sorted(hook_dir.iterdir()):
        if path.is_file() and path.suffix in HOOK_CODE_SUFFIXES:
            text = path.read_bytes().replace(str(slot.root).encode(), _SLOT.encode())
            digest.update(path.name.encode() + b"\0" + text + b"\0")
    return digest.hexdigest()


def files_digests(paths: Mapping[str, Path], slot: Slot) -> dict[str, str | None]:
    def digest(name: str) -> str | None:
        path = paths.get(name)
        return sha256_text(_portable(path.read_text(encoding="utf-8"), slot)) if path else None

    return {
        "mcp_config_sha256": digest("mcp.json"),
        "hook_settings_sha256": digest("settings.json"),
        "hook_scripts_sha256": hook_scripts_digest(slot) if "settings.json" in paths else None,
    }


def actor_argv(config: RunConfig, spec: ArmSpec, paths: Mapping[str, Path]) -> list[str]:
    # No harness client instructions: f34 measures the shipped default
    # surface. No built-in tools: the actor reaches the vault only through Exomem.
    arm = Arm(
        arm_id=spec.arm_id,
        prominence="shipped-default",
        tools="",
        allowed_tools=spec.allowed_tools_flag(SERVER_NAME),
        uses_plugin=False,
        uses_custom_instructions=False,
        configure_ref=f"organisation-replay-{spec.arm_id}",
    )
    return build_turn_argv(
        executable=config.client.executable,
        arm=arm,
        prompt=None,
        mcp_config=paths["mcp.json"],
        session_id=None,
        model=config.model,
        max_turns=None,
        first=True,
        append_system_prompt_file=None,
        plugin_dir=None,
        settings_file=paths.get("settings.json"),
    )


def _child(slot: Slot, spec: ArmSpec, *args: str) -> dict:
    """Run the readback module under this interpreter with the slot's product env."""

    out = slot.root.parent / f"readback-{os.getpid()}-{time.monotonic_ns()}.json"
    done = subprocess.run(  # noqa: S603 - this interpreter, the package's own module
        [
            sys.executable,
            "-m",
            "membench.utility.organisation_replay.readback",
            *args,
            "--out",
            str(out),
        ],
        env=_child_env(os.environ, slot, spec),
        cwd=slot.cwd,
        capture_output=True,
        text=True,
    )
    if done.returncode != 0:
        raise StateError(f"readback {args[0]} failed: {done.stderr[-1500:]}")
    try:
        return json.loads(out.read_text(encoding="utf-8"))
    finally:
        out.unlink(missing_ok=True)


def fresh_slot(slot: Slot, spec: ArmSpec) -> None:
    slot.wipe()
    for path in (slot.vault, slot.state, slot.runtime, slot.cwd, slot.hook_home):
        path.mkdir(parents=True)
    done = subprocess.run(  # noqa: S603 - this interpreter, the product's own init
        [
            sys.executable,
            "-c",
            "import pathlib, sys; from exomem.init import init_vault; init_vault(pathlib.Path(sys.argv[1]))",
            str(slot.vault),
        ],
        env=_child_env(os.environ, slot, spec),
        cwd=slot.cwd,
        capture_output=True,
        text=True,
    )
    if done.returncode != 0:
        raise StateError(f"vault init failed: {done.stderr[-1500:]}")


def _sha_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


# --------------------------------------------------------------------------
# Binding: once per arm and run.
# --------------------------------------------------------------------------


def _git(*args: str) -> bytes:
    """One git command in this checkout's repository; a failure refuses the bind."""

    done = subprocess.run(  # noqa: S603 - git with validated 40-hex revisions only
        ["git", "-C", str(_CHECKOUT), *args], capture_output=True
    )
    if done.returncode != 0:
        raise SystemExit(
            f"refused: git {' '.join(args)}: {done.stderr.decode(errors='replace').strip()}"
        )
    return done.stdout


def bind(config: RunConfig) -> dict:
    """The arm's binding record (SCHEMA.md section 3, item 2), from a slot of its own."""

    if config.role not in REASON_CAPABLE:
        raise SystemExit(f"refused: role {config.role!r} is not one of {sorted(REASON_CAPABLE)}")
    for name in ("revision", "base_revision"):
        if not _REVISION.fullmatch(getattr(config, name)):
            raise SystemExit(f"refused: {name} {getattr(config, name)!r} is not 40 hex characters")
    # A mislabelled arm would bind the other writer's reason capability: the
    # baseline is the base revision itself, and the candidate is a change on it.
    if (config.role == "candidate") == (config.revision == config.base_revision):
        raise SystemExit(
            f"refused: a {config.role} arm at {config.revision} contradicts the base "
            f"revision {config.base_revision}"
        )
    merge_base = _git("merge-base", config.revision, config.base_revision).decode().strip()
    if merge_base != config.base_revision:
        raise SystemExit(
            f"refused: the arm's revision forks from {merge_base}, not from the base "
            f"revision {config.base_revision}"
        )
    refuse_api_keys(os.environ)
    root = refuse_unsafe_out_dir(config.out) / f"binding-{config.arm}"
    slot = Slot(root / "slot")
    probe = arm_spec(config, "hookless", ())
    fresh_slot(slot, probe)

    async def discover() -> tuple[tuple[str, ...], dict]:
        child_env = _child_env(os.environ, slot, probe)
        async with ScriptedSession(slot, probe, env=child_env, log_dir=root, name="bootstrap") as s:
            return tuple(sorted(s.doors.served)), await s.call("bootstrap", {})

    import asyncio

    tools, bootstrap = asyncio.run(discover())
    shapes = {}
    for shape in ("hookless", "hooked"):
        spec = arm_spec(config, shape, tools)
        surface = capture_surface(
            slot, spec, env=_child_env(os.environ, slot, spec), out=root / f"surface-{shape}"
        )
        paths, wired = session_files(
            slot, spec, root / f"session-files-{shape}", hooked=shape == "hooked"
        )
        shapes[shape] = {
            "surface": surface,
            "tools_list_sha256": surface["tool_descriptions"],
            "instructions_sha256": surface["server_instructions"],
            "tools": list(tools),
            "wired_hooks": sorted(wired),
            **files_digests(paths, slot),
        }
    facts = _child(slot, probe, "binding", "--surface", ACTOR_SURFACE)
    # The role follows the revision label, so the code that loaded must be that
    # revision. A wheel installed from a file names no revision; its digest stands in.
    loaded = facts["package_identity"].get("source_revision")
    if loaded is not None and loaded != config.revision:
        raise SystemExit(
            f"refused: the arm's interpreter loads the package at {loaded}, not at the "
            f"bound revision {config.revision}"
        )
    record = {
        "binding_ref": f"{config.arm}-{config.revision[:12]}",
        "arm": config.arm,
        "revision": config.revision,
        "base_revision": config.base_revision,
        "merge_base": merge_base,
        # The measurement lane recomputes this to check the arm is the reviewed head;
        # the base arm's diff is empty.
        "diff_sha256": hashlib.sha256(
            _git("diff", "--binary", f"{config.base_revision}..{config.revision}")
        ).hexdigest(),
        "reason_capable": REASON_CAPABLE[config.role],
        "wheel_sha256": _sha_file(config.wheel)
        if config.wheel
        else "unavailable: no wheel given (source checkout)",
        "product_version": facts["product_version"],
        "interpreters": {
            # The server, the tap and the seed builder all run on this interpreter.
            "server_tap_seed": {"path": sys.executable, "version": sys.version},
            "readback": facts["interpreter"],
        },
        "package_identity": {
            "read_in": "a child of the server's interpreter with the server's environment",
            "loaded_source_path": facts["loaded_source_path"],
            **facts["package_identity"],
        },
        "contract_version": bootstrap.get("contract_version"),
        "scaffold": facts["scaffold"],
        "engagement": {
            "served_by_bootstrap": bootstrap.get("engagement"),
            "authority_mode": facts["authority_mode"],
            "resolved_prominence": {shape: facts["engagement"][ACTOR_SURFACE] for shape in shapes},
            "actor_surface": ACTOR_SURFACE,
        },
        "client": {
            "executable": str(config.client.executable),
            "version": config.client.version,
            "launcher": list(config.launcher),
            "model": config.model,
            "permission_mode": "client default (no permission flag in the argv)",
            "standin": config.standin,
        },
        "profile": config.profile,
        "packet_mode": PACKET_MODE,
        "call_guard": CALL_GUARD,
        "shapes": shapes,
    }
    (config.out / f"binding-{config.arm}.json").write_text(
        json.dumps(record, indent=2, sort_keys=True), encoding="utf-8"
    )
    return record


# --------------------------------------------------------------------------
# One situation.
# --------------------------------------------------------------------------


def _fault(session: str, attempt: int, kind: str, detail: str) -> dict:
    return {"session": session, "attempt": attempt, "kind": kind, "detail": detail}


def _build_seed(
    config: RunConfig, situation: Situation, slot: Slot, spec: ArmSpec, run_dir: Path, faults: list
):
    import asyncio

    for attempt in range(1, SEED_RETRIES + 2):
        log_dir = run_dir / "seed" / f"attempt-{attempt}"
        try:
            fresh_slot(slot, spec)
            build = asyncio.run(
                seed_module.build_seed(
                    slot, spec, situation, env=_child_env(os.environ, slot, spec), log_dir=log_dir
                )
            )
            facts = _child(slot, spec, "facts")
            problems = seed_module.check_seed(situation, build, facts)
            (log_dir / "build.json").write_text(
                json.dumps(build.__dict__, indent=2), encoding="utf-8"
            )
            if not problems:
                return build, facts
            faults.append(_fault("seed", attempt, HARNESS_FAULT, "; ".join(problems)))
        except (DoorFault, seed_module.SeedFault, StateError, OSError) as error:
            faults.append(
                _fault("seed", attempt, HARNESS_FAULT, f"{type(error).__name__}: {error}")
            )
    return None, None


def _snapshot_slot(slot: Slot, to: Path) -> None:
    # Copy beside the target and rename, so an interrupted copy never reads as a snapshot.
    partial = to.with_name(f"{to.name}.partial")
    for stale in (partial, to):
        if stale.exists():
            shutil.rmtree(stale)
    shutil.copytree(slot.root, partial, symlinks=True)
    partial.rename(to)


def _restore_slot(slot: Slot, source: Path) -> None:
    slot.wipe()
    shutil.copytree(source, slot.root, symlinks=True)


def _write_json(path: Path, value: object) -> Path:
    """Write a whole file or nothing, so an interruption never leaves half a record."""

    partial = path.with_name(f"{path.name}.partial")
    partial.write_text(json.dumps(value, indent=1, sort_keys=True, default=str), encoding="utf-8")
    partial.replace(path)
    return path


def _session(config, situation, role, turns, slot, spec, run_dir, executor, paths):
    """Run one measured session to its final outcome, resuming an interrupted one.

    The slot snapshot taken before the first attempt stays until the session
    ends: every attempt, and every later invocation that resumes this session,
    starts from it, so the inputs digest matches the attempts already logged.
    """

    session_dir = run_dir / "sessions" / role
    pristine = run_dir / f"pre-{role}"
    final = load_session(session_dir)
    if final is not None:
        # An earlier invocation finished this session; the slot holds its end state.
        if pristine.exists():
            shutil.rmtree(pristine)
        return final, session_dir
    if pristine.exists():
        _restore_slot(slot, pristine)
    else:
        # Each session starts with an empty runtime: its own tap log, server stderr and turn file.
        shutil.rmtree(slot.runtime)
        slot.runtime.mkdir()
    planned = PlannedSession(
        session_id=f"{situation.id}--{config.arm}--{role}",
        case_id=situation.id,
        arm=config.arm,
        repeat=0,
        position=0,
    )
    inputs = SessionInputs(
        planned=planned,
        tree=role,
        argv=tuple(actor_argv(config, spec, paths)),
        env=session_env(os.environ, slot, spec),
        cwd=slot.cwd,
        turns=tuple(turns),
        scored_turn=len(turns) - 1,
        files={
            name: _portable(path.read_text(encoding="utf-8"), slot) for name, path in paths.items()
        },
        state_digests={"vault": tree_digest(slot.vault)},
    )
    if not pristine.exists():
        _snapshot_slot(slot, pristine)

    def execute(inputs: SessionInputs, attempt_dir: Path):
        # A retry keeps its inputs: each attempt starts from the same slot.
        _restore_slot(slot, pristine)
        started = dt.datetime.now(dt.UTC)
        try:
            return executor.execute_prepared(inputs, attempt_dir, slot)
        finally:
            # The readback window of the session's last attempt, kept on disk for a resume.
            ended = dt.datetime.now(dt.UTC)
            _write_json(session_dir / WINDOW_FILE, [started.isoformat(), ended.isoformat()])

    final = run_session(inputs, session_dir, execute)
    shutil.rmtree(pristine)
    return final, session_dir


def _session_faults(role: str, session_dir: Path) -> list[dict]:
    return [
        _fault(role, row["attempt"], row["outcome"], f"{row.get('kind')}: {row.get('detail')}")
        for row in read_jsonl(session_dir / "attempts.jsonl")
        if row.get("outcome") != OK
    ]


def _now() -> str:
    return dt.datetime.now(dt.UTC).isoformat()


def run_situation(config: RunConfig, binding: Mapping, situation: Situation) -> Path:
    """Run one situation for one arm, or resume it from its checkpoint.

    ``run.json`` is written once, when the situation reaches ``ok``,
    ``actor_failure`` or ``struck``; only a harness fault past its retries
    strikes it. Until then ``progress.json`` holds the run as ``paused`` with
    the stage to resume: a quota pause, an abort or a crash leaves it, and the
    next invocation continues from that stage on the preserved slot.
    """

    refuse_api_keys(os.environ)
    run_dir = refuse_unsafe_out_dir(config.out) / situation.id / config.arm
    if not isinstance(binding.get("reason_capable"), bool):
        raise SystemExit(f"refused: binding {binding['binding_ref']} has no reason_capable; rebind")
    if (run_dir / "run.json").exists():
        raise SystemExit(f"refused: {run_dir / 'run.json'} exists; a finished run is written once")
    run_dir.mkdir(parents=True, exist_ok=True)
    shape = binding["shapes"][situation.client_shape]
    spec = arm_spec(
        config, situation.client_shape, tuple(shape["tools"]), frozenset(shape["wired_hooks"])
    )
    slot = Slot(run_dir / "slot")
    progress_path = run_dir / PROGRESS_FILE
    if progress_path.exists():
        progress = json.loads(progress_path.read_text(encoding="utf-8"))
        run = progress["run"]
        if (run["situation_sha256"], run["binding_ref"]) != (
            situation.sha256,
            binding["binding_ref"],
        ):
            raise SystemExit(
                f"refused: {progress_path} was started from another situation file or binding"
            )
        run["interruptions"].append({"stage": progress["stage"], "event": "resumed", "at": _now()})
    else:
        # Only the model-free seed runs before the first checkpoint, so an
        # interrupted seed is rebuilt from a clean slot.
        leftovers = [run_dir / name for name in ("seed", "slot") if (run_dir / name).exists()]
        for path in leftovers:
            shutil.rmtree(path)
        run = {
            "situation_id": situation.id,
            "situation_sha256": situation.sha256,
            "pool": situation.pool,
            "category": situation.category,
            "variant": situation.variant,
            "arm": config.arm,
            "client_shape": situation.client_shape,
            "binding_ref": binding["binding_ref"],
            "seed": {},
            "sessions": [],
            "end_state": {},
            "faults": [],
            "interruptions": [{"stage": "seed", "event": "restarted", "at": _now()}]
            if leftovers
            else [],
            "status": PAUSED,
        }
        progress = {"stage": "seed", "run": run}
    faults = run["faults"]

    def checkpoint(stage: str) -> None:
        progress["stage"] = stage
        _write_json(progress_path, progress)

    def finish(status: str) -> Path:
        run["status"] = status
        path = _write_json(run_dir / "run.json", run)
        progress_path.unlink(missing_ok=True)
        return path

    seed_file = run_dir / "seed-pages.json"
    if progress["stage"] == "seed":
        build, facts = _build_seed(config, situation, slot, spec, run_dir, faults)
        if build is None:
            slot.wipe()
            return finish("struck")
        run["seed"] = {
            **seed_module.seed_snapshot(facts, tree_digest=tree_digest(slot.vault), check=[]),
            "relation_keys": build.relation_keys,
            "home_keys": build.home_keys,
            "prior_entries": build.prior_entries,
            "settle_calls": build.settle_calls,
            "settle_log": build.settle_log,
            "landed": {"entities": build.entities, "notes": build.notes},
            "wraps": build.wraps,
            "wrap_rule": seed_module.WRAP_RULE,
            "retries": build.retries,
        }
        _write_json(seed_file, {page["path"]: page["content_sha256"] for page in facts["pages"]})
        checkpoint("capture")
    paths, wired = session_files(
        slot, spec, run_dir / "session-inputs", hooked=situation.client_shape == "hooked"
    )
    digests = files_digests(paths, slot)
    if digests != {k: shape.get(k) for k in digests} or wired != spec.wired_hooks:
        raise SystemExit(
            f"refused: the session files moved since binding: {digests}, hooks {sorted(wired)}"
        )
    executor = ClaudeSessionExecutor(
        layout=Layout(run_dir),
        specs={config.arm: spec},
        model=config.model,
        client_version=config.client.version.split()[0],
        surfaces={(config.arm, role): shape["surface"] for role in ("capture", "fresh")},
        launcher=config.launcher,
    )
    try:
        if progress["stage"] == "capture":
            capture, capture_dir = _session(
                config, situation, "capture", situation.turns, slot, spec, run_dir, executor, paths
            )
            faults += _session_faults("capture", capture_dir)
            run["sessions"].append(session_object("capture", capture, capture_dir, run_dir))
            if capture["outcome"] == HARNESS_FAULT:
                return finish("struck")
            before = tree_digest(slot.vault)
            since, until = json.loads((capture_dir / WINDOW_FILE).read_text(encoding="utf-8"))
            run["end_state"] = _child(
                slot,
                spec,
                "end-state",
                "--since",
                since,
                "--until",
                until,
                "--seed-pages",
                str(seed_file),
                *(["--reason-capable"] if binding["reason_capable"] else []),
            )
            run["end_state"]["read"]["vault_digest_before"] = before
            run["end_state"]["read"]["vault_digest_after"] = tree_digest(slot.vault)
            if run["end_state"]["read"]["vault_digest_after"] != before:
                faults.append(
                    _fault("readback", 1, HARNESS_FAULT, "the readback changed the vault")
                )
                return finish("struck")
            checkpoint("fresh")
        fresh, fresh_dir = _session(
            config, situation, "fresh", (situation.question,), slot, spec, run_dir, executor, paths
        )
        faults += _session_faults("fresh", fresh_dir)
        run["sessions"].append(session_object("fresh", fresh, fresh_dir, run_dir))
    except (RunAborted, RunPaused) as stop:
        # A pause or an abort is not the situation's outcome: the run stays resumable.
        run["interruptions"].append(
            {
                "stage": progress["stage"],
                "event": QUOTA if isinstance(stop, RunPaused) else ABORT,
                "detail": str(stop),
                "at": _now(),
            }
        )
        checkpoint(progress["stage"])
        raise
    outcomes = [session["outcome"] for session in run["sessions"]]
    status = (
        "struck"
        if HARNESS_FAULT in outcomes
        else "actor_failure"
        if ACTOR_FAILURE in outcomes
        else "ok"
    )
    path = finish(status)
    slot.wipe()
    return path
