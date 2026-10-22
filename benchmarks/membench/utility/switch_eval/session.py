"""Sessions: the plan, one headless session, its classification and the retry rule.

A session is one agent CLI process in streaming-input mode: the harness writes
the case's quoted user turns to its stdin one at a time, waits for each turn's
``result`` event, and closes stdin after the scored turn. One process means one
fresh session and one Exomem server process for the whole case (design,
"Session"), behind the tap that enforces the pull budget.

Classification reads structured fields only: the CLI's event types, its
documented error kinds and the tap's log. A crash of the server under test is
an actor failure and is never retried; a rate limit, a transport error or a
host fault is a harness fault and is retried with the same inputs, up to the
protocol's limit; a spent quota pauses the run (design, "Runs and failures").
"""

from __future__ import annotations

import json
import os
import queue
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path

from epistemic.journeys.f27_replay import (
    AgentEnvelope,
    build_turn_argv,
    environment_delta,
    environment_floor,
)
from membench.trackc.witness_join import parse_stream_json_transcript
from membench.utility.context_activation_arms import rotate_arm_order

from . import protocol
from .arms import ArmSpec, canonical_json, client_instructions, f27_arm, hook_settings, sha256_text
from .cases import Case
from .state import (
    SERVER_NAME,
    Layout,
    Slot,
    StateError,
    mcp_server_entry,
    prepare_session_slot,
    product_env,
)

# Outcomes of one attempt. ``ok`` and ``actor_failure`` are final; a
# ``harness_fault`` is retried; ``quota`` pauses the run; ``abort`` stops it,
# because the setup itself is wrong and every later session would share it.
OK = "ok"
ACTOR_FAILURE = "actor_failure"
HARNESS_FAULT = "harness_fault"
QUOTA = "quota"
ABORT = "abort"
#: A session never run because its case was struck after harness faults.
STRUCK = "struck"

#: Billing variables a subscription run must not carry (design, "Actor": "on
#: the subscription with no API key in the environment"). The CLI's documented
#: names; the init event's ``apiKeySource`` is checked as well.
API_KEY_VARIABLES: tuple[str, ...] = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")

#: Assistant ``error`` kinds the CLI documents. A rate limit, a server error or
#: an unknown error is the provider's or the transport's; authentication and
#: billing errors, and a refused request, are the harness's setup.
_FAULT_ERRORS = {"rate_limit": "rate_limit", "server_error": "transport", "unknown": "transport"}
_ABORT_ERRORS = frozenset({"authentication_failed", "billing_error", "invalid_request"})

#: Seconds the harness waits for the CLI to exit after the scored turn before
#: it ends the process group. Not a protocol value: the session is over.
_EXIT_GRACE_SECONDS = 60.0

#: The user's own state root, watched for writes a session should never make.
_USER_STATE_ROOT = Path.home() / ".local" / "state" / "exomem"


class RunAborted(RuntimeError):
    """A setup error every later session would share; the run stops."""


class RunPaused(RuntimeError):
    """A spent quota; the run resumes later with unchanged inputs."""


@dataclass(frozen=True)
class PlannedSession:
    session_id: str
    case_id: str
    arm: str
    repeat: int
    position: int


def session_id_for(case_id: str, arm: str, repeat: int) -> str:
    return f"{case_id}--{arm}--r{repeat}"


def plan_sessions(
    cases: Sequence[Case], *, arm_ids: Sequence[str], repeats: int, seed: int
) -> list[PlannedSession]:
    """Every session, in run order: repeats outermost, arms rotated per case.

    Running each repeat across all cases before the next one interleaves the
    arms in time, and the rotation changes which arm a case meets first
    (design, "Order").
    """

    planned: list[PlannedSession] = []
    for repeat in range(1, repeats + 1):
        for index, case in enumerate(cases):
            for arm in rotate_arm_order(seed + repeat, index, arm_ids=tuple(arm_ids)):
                planned.append(
                    PlannedSession(
                        session_id_for(case.case_id, arm, repeat),
                        case.case_id,
                        arm,
                        repeat,
                        len(planned),
                    )
                )
    return planned


@dataclass(frozen=True)
class ArmFiles:
    """The files an arm's sessions read, written once per arm and hashed."""

    mcp_config: Path
    instructions: Path
    settings: Path | None
    contents: Mapping[str, str]


def arm_files(layout: Layout, spec: ArmSpec) -> ArmFiles:
    """The arm's MCP config, client instructions and hook settings, as content.

    Every path inside them is a slot path, so every session of the arm reads
    byte-identical files and a retry's inputs are the first attempt's inputs.
    The server entry is the harness's own rather than the shared trackc
    writer's: that writer pins four locations and cannot put the tap in front.
    """

    slot = Slot(layout.slot)
    base = layout.arm_dir(spec.arm_id) / "session-inputs"
    entry = mcp_server_entry(
        slot, spec, log=slot.runtime / "tap.jsonl", server_stderr=slot.runtime / "server.stderr"
    )
    contents = {
        "mcp.json": json.dumps({"mcpServers": {SERVER_NAME: entry}}, indent=2, sort_keys=True)
    }
    contents["instructions.md"] = client_instructions(spec)[0]
    settings = hook_settings(spec, hook_home=slot.hook_home)
    if settings is not None:
        contents["settings.json"] = json.dumps(settings, indent=2, sort_keys=True)
    return ArmFiles(
        mcp_config=base / "mcp.json",
        instructions=base / "instructions.md",
        settings=base / "settings.json" if settings is not None else None,
        contents=contents,
    )


def write_arm_files(files: ArmFiles) -> None:
    files.mcp_config.parent.mkdir(parents=True, exist_ok=True)
    for name, text in files.contents.items():
        path = files.mcp_config.parent / name
        if not path.exists() or path.read_text(encoding="utf-8") != text:
            path.write_text(text, encoding="utf-8")


def session_env(parent: Mapping[str, str], slot: Slot, spec: ArmSpec) -> dict[str, str]:
    """The child environment: the floor, no inherited product settings, the pins.

    ``KB_*`` are the retrieve hook's legacy names for its own settings; an
    inherited one would change the push. ``PATH`` starts at the build's
    interpreter directory, so the hook's ``python3`` and its CLI rung's
    ``exomem`` are this build. The CLI rung stands in for the deployed service
    transport, which a session must never reach.
    """

    floor, _removed = environment_floor(parent)
    env = {key: value for key, value in floor.items() if not key.startswith(("EXOMEM_", "KB_"))}
    env.update(product_env(slot, packet_mode=spec.packet_mode))
    env["EXOMEM_RETRIEVE_INJECT_CLI"] = "1"
    build_bin = str(Path(sys.executable).parent)
    rest = [entry for entry in floor.get("PATH", "").split(os.pathsep) if entry and entry != build_bin]
    env["PATH"] = os.pathsep.join([build_bin, *rest])
    return env


@dataclass(frozen=True)
class SessionInputs:
    """Everything that defines one session. A retry must reproduce its digest."""

    planned: PlannedSession
    tree: str
    argv: tuple[str, ...]
    env: Mapping[str, str]
    cwd: Path
    turns: tuple[str, ...]
    scored_turn: int
    files: Mapping[str, str]
    state_digests: Mapping[str, str]

    def digest(self) -> str:
        # The harness-controlled part of the environment: what the floor
        # removed is reported by the dry run, and the operator's own variables
        # (a terminal's, a pane's) are not inputs of the measurement.
        product = {
            key: self.env[key]
            for key in sorted(self.env)
            if key.startswith("EXOMEM_") or key == "PATH"
        }
        return sha256_text(
            canonical_json(
                {
                    "case": self.planned.case_id,
                    "arm": self.planned.arm,
                    "tree": self.tree,
                    "argv": list(self.argv),
                    "env": product,
                    "cwd": str(self.cwd),
                    "turns": list(self.turns),
                    "scored_turn": self.scored_turn,
                    "files": {name: sha256_text(text) for name, text in sorted(self.files.items())},
                    "state": dict(self.state_digests),
                }
            )
        )


def session_argv(
    envelope: AgentEnvelope, spec: ArmSpec, files: ArmFiles, *, model: str
) -> list[str]:
    """The session's invocation, from the shared f27 argv builder.

    Streaming input (no prompt argument), no persisted session (no id), no
    agentic-turn cap (the protocol bounds a turn by its pull budget and its
    timeout, not by a step count), the arm's hook settings when it pushes.
    """

    return build_turn_argv(
        executable=envelope.executable,
        arm=f27_arm(spec, server_name=SERVER_NAME),
        prompt=None,
        mcp_config=files.mcp_config,
        session_id=None,
        model=model,
        max_turns=None,
        first=True,
        append_system_prompt_file=files.instructions,
        plugin_dir=None,
        settings_file=files.settings,
    )


def session_inputs(
    *,
    layout: Layout,
    planned: PlannedSession,
    case: Case,
    spec: ArmSpec,
    envelope: AgentEnvelope,
    model: str,
    parent_env: Mapping[str, str],
    state_digests: Mapping[str, str],
) -> SessionInputs:
    files = arm_files(layout, spec)
    slot = Slot(layout.slot)
    return SessionInputs(
        planned=planned,
        tree=case.tree,
        argv=tuple(session_argv(envelope, spec, files, model=model)),
        env=session_env(parent_env, slot, spec),
        cwd=slot.cwd,
        # The turns after the scored one cannot change its answer, so the
        # session ends at the scored turn.
        turns=case.turns[: case.scored_turn + 1],
        scored_turn=case.scored_turn,
        files=dict(files.contents),
        state_digests=dict(state_digests),
    )


def dry_run_lines(inputs: SessionInputs, *, parent_env: Mapping[str, str]) -> list[str]:
    """One session, as the existing dry-run contract prints a plan: never executed."""

    import shlex

    removed, added, changed = environment_delta(dict(parent_env), dict(inputs.env))
    planned = inputs.planned
    return [
        f"session {planned.position + 1}: {planned.session_id}  arm {planned.arm}  repeat {planned.repeat}  tree {inputs.tree}",
        f"  cwd it would run from: {inputs.cwd}",
        f"  env removed: {', '.join(removed) or 'nothing'}",
        f"  env added:   {', '.join(added) or 'nothing'}",
        f"  env changed: {', '.join(changed) or 'nothing'}",
        f"  turns it would play: {len(inputs.turns)} (scored turn {inputs.scored_turn + 1})",
        f"  inputs digest: {inputs.digest()}",
        f"  argv: {shlex.join(inputs.argv)}",
    ]


# --------------------------------------------------------------------------
# One attempt.
# --------------------------------------------------------------------------


@dataclass
class AttemptResult:
    outcome: str
    kind: str = ""
    detail: str = ""
    answer: str | None = None
    turns: list[dict] = field(default_factory=list)
    resets_at: object = None
    checks: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return asdict(self)


def _user_message(text: str) -> str:
    return json.dumps(
        {
            "type": "user",
            "message": {"role": "user", "content": text},
            "parent_tool_use_id": None,
            "session_id": "default",
        }
    )


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def hook_push(event: Mapping) -> str:
    """The text a ``UserPromptSubmit`` hook response added to the turn."""

    for raw in (event.get("stdout"), event.get("output")):
        if not isinstance(raw, str) or not raw.strip():
            continue
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return raw.strip()
        if isinstance(payload, dict):
            specific = payload.get("hookSpecificOutput") or {}
            text = specific.get("additionalContext") if isinstance(specific, dict) else None
            return text if isinstance(text, str) else ""
    return ""


def _fallback_push() -> str:
    """The retrieve hook's own text for a working-set ladder that reached nothing."""

    from exomem._hooks import exomem_retrieve_nudge

    return exomem_retrieve_nudge._MCP_ACTIVATION  # noqa: SLF001 - the hook's own fallback constant


def classify(
    *,
    spec: ArmSpec,
    model: str,
    client_version: str,
    events: Sequence[dict],
    turn_records: Sequence[dict],
    tap_events: Sequence[dict],
    scored_turn: int,
    timed_out: bool,
    quota: dict | None,
    surface: Mapping[str, str] | None,
) -> AttemptResult:
    """One attempt's outcome, from structured fields only. Pure, so it is tested
    on the tap's real log."""

    checks: dict[str, object] = {}
    if quota is not None:
        return AttemptResult(
            QUOTA,
            "quota",
            json.dumps(quota),
            resets_at=quota.get("resetsAt"),
            turns=list(turn_records),
        )

    crash = [
        e for e in tap_events if e.get("event") == "server_exit" and e.get("while_client_connected")
    ]
    if crash:
        return AttemptResult(
            ACTOR_FAILURE,
            "server_crash",
            f"server exited {crash[0].get('returncode')}",
            turns=list(turn_records),
        )

    for event in events:
        if event.get("type") == "assistant" and event.get("error"):
            kind = str(event["error"])
            if kind in _ABORT_ERRORS:
                return AttemptResult(
                    ABORT, kind, "the CLI refused the session's setup", turns=list(turn_records)
                )
            if kind in _FAULT_ERRORS:
                return AttemptResult(
                    HARNESS_FAULT, _FAULT_ERRORS[kind], kind, turns=list(turn_records)
                )

    init = next(
        (e for e in events if e.get("type") == "system" and e.get("subtype") == "init"), None
    )
    if init is not None:
        expected_tools = sorted(f"mcp__{SERVER_NAME}__{tool}" for tool in spec.tools)
        servers = {
            s.get("name"): s.get("status")
            for s in init.get("mcp_servers") or []
            if isinstance(s, dict)
        }
        checks.update(
            tools=sorted(init.get("tools") or []) == expected_tools,
            model=init.get("model") == model,
            client_version=str(init.get("claude_code_version")) == client_version,
            api_key_source=init.get("apiKeySource") == "none",
            server_connected=servers.get(SERVER_NAME) == "connected",
        )
        if not checks["api_key_source"]:
            return AttemptResult(
                ABORT,
                "api_key",
                f"apiKeySource {init.get('apiKeySource')!r}",
                turns=list(turn_records),
            )
        if not checks["server_connected"]:
            started = any(e.get("event") == "server_started" for e in tap_events)
            # The tap never ran, or never reached the server: the transport.
            kind = "transport" if not started else "server_start"
            outcome = HARNESS_FAULT if kind == "transport" else ACTOR_FAILURE
            return AttemptResult(
                outcome,
                kind,
                f"server status {servers.get(SERVER_NAME)!r}",
                turns=list(turn_records),
            )
        for name in ("tools", "model", "client_version"):
            if not checks[name]:
                return AttemptResult(
                    ABORT,
                    f"surface_{name}",
                    "the session's surface is not the arm's",
                    checks=checks,
                )
    if surface is not None:
        initialize = next((e for e in tap_events if e.get("event") == "initialize"), None)
        listing = next((e for e in tap_events if e.get("event") == "tools_list"), None)
        if initialize is not None:
            checks["server_instructions"] = (
                sha256_text(initialize.get("instructions") or "") == surface["server_instructions"]
            )
        if listing is not None:
            tools = sorted(listing.get("tools") or [], key=lambda tool: tool.get("name", ""))
            checks["tool_descriptions"] = (
                sha256_text(canonical_json(tools)) == surface["tool_descriptions"]
            )
        for name in ("server_instructions", "tool_descriptions"):
            if checks.get(name) is False:
                return AttemptResult(
                    ABORT,
                    f"surface_{name}",
                    "the server's surface moved after the state stage",
                    checks=checks,
                )

    if spec.hook_mode is not None:
        fallback = _fallback_push() if spec.hook_mode == "working-set" else None
        for record in turn_records:
            responses = record.get("hook_responses") or []
            if not responses:
                return AttemptResult(
                    HARNESS_FAULT,
                    "push_missing",
                    f"turn {record['index'] + 1}",
                    turns=list(turn_records),
                    checks=checks,
                )
            for response in responses:
                if response.get("exit_code") not in (None, 0):
                    return AttemptResult(
                        HARNESS_FAULT,
                        "push_failed",
                        f"turn {record['index'] + 1}",
                        turns=list(turn_records),
                        checks=checks,
                    )
                if fallback is not None and response.get("push") == fallback:
                    # The working-set ladder reached no transport and pushed its
                    # floor: the stand-in for the service failed, not the arm.
                    return AttemptResult(
                        HARNESS_FAULT,
                        "push_transport",
                        f"turn {record['index'] + 1}",
                        turns=list(turn_records),
                        checks=checks,
                    )

    if timed_out:
        return AttemptResult(ACTOR_FAILURE, "timeout", "", turns=list(turn_records), checks=checks)
    scored = next((r for r in turn_records if r["index"] == scored_turn), None)
    if scored is None or scored.get("result_subtype") is None:
        return AttemptResult(
            HARNESS_FAULT,
            "host",
            "the CLI ended before the scored turn's result",
            turns=list(turn_records),
            checks=checks,
        )
    if scored["result_subtype"] != "success" or scored.get("is_error"):
        return AttemptResult(
            HARNESS_FAULT,
            "host",
            str(scored["result_subtype"]),
            turns=list(turn_records),
            checks=checks,
        )
    answer = scored.get("answer") or ""
    if not answer.strip():
        return AttemptResult(
            ACTOR_FAILURE, "no_answer", "empty answer", turns=list(turn_records), checks=checks
        )
    return AttemptResult(OK, "", "", answer=answer, turns=list(turn_records), checks=checks)


def _turn_record(
    index: int,
    lines: Sequence[str],
    events: Sequence[dict],
    tap_events: Sequence[dict],
    wall: float,
) -> dict:
    transcript = parse_stream_json_transcript(lines)
    result = next((e for e in reversed(events) if e.get("type") == "result"), None)
    hooks = [
        {"exit_code": e.get("exit_code"), "outcome": e.get("outcome"), "push": hook_push(e)}
        for e in events
        if e.get("type") == "system"
        and e.get("subtype") == "hook_response"
        and e.get("hook_event") == "UserPromptSubmit"
    ]
    calls = [e for e in tap_events if e.get("event") == "call" and e.get("turn") == index]
    usage = transcript.usage or {}
    return {
        "index": index,
        "wall_seconds": round(wall, 3),
        "result_subtype": result.get("subtype") if result else None,
        "is_error": bool(result.get("is_error")) if result else None,
        "answer": transcript.result_text if result else None,
        "usage": usage,
        "input_tokens": sum(
            int(usage.get(k) or 0)
            for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
        ),
        "output_tokens": int(usage.get("output_tokens") or 0),
        "hook_responses": hooks,
        "push_bytes": sum(len(h["push"].encode("utf-8")) for h in hooks),
        "calls": len(calls),
        "calls_served": sum(1 for c in calls if c.get("outcome") == "served"),
        "calls_refused_budget": sum(1 for c in calls if c.get("outcome") == "refused_budget"),
        "calls_refused_filtered": sum(1 for c in calls if c.get("outcome") == "refused_filtered"),
    }


def _end_process_group(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    for sig, wait in ((signal.SIGTERM, 10.0), (signal.SIGKILL, 10.0)):
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            return
        try:
            proc.wait(timeout=wait)
            return
        except subprocess.TimeoutExpired:
            continue


def _end_orphan_server(tap_events: Sequence[dict], slot: Slot) -> bool:
    """End the session's own server if it outlived the CLI; never any other process."""

    started = next((e for e in tap_events if e.get("event") == "server_started"), None)
    if started is None:
        return False
    pid = int(started["pid"])
    try:
        environ = Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
    except OSError:
        return False
    if f"EXOMEM_STATE_ROOT={slot.state}".encode() not in environ:
        return False
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return False
    return True


def _user_state_writes(since: float) -> list[str]:
    """Files under the user's own state root changed since ``since``: a warning
    that a location escaped the pins, never a failure (another process may own
    them)."""

    if not _USER_STATE_ROOT.exists():
        return []
    found: list[str] = []
    for root, _dirs, names in os.walk(_USER_STATE_ROOT):
        for name in names:
            path = Path(root) / name
            try:
                if path.stat().st_mtime >= since:
                    found.append(str(path))
            except OSError:
                continue
            if len(found) >= 20:
                return found
    return found


@dataclass
class ClaudeSessionExecutor:
    """Runs one attempt for real. Each attempt starts from a fresh state copy."""

    layout: Layout
    specs: Mapping[str, ArmSpec]
    model: str
    client_version: str
    surfaces: Mapping[tuple[str, str], Mapping[str, str]]
    turn_timeout: float = protocol.TURN_TIMEOUT_SECONDS

    def __call__(self, inputs: SessionInputs, attempt_dir: Path) -> AttemptResult:
        spec = self.specs[inputs.planned.arm]
        try:
            slot = prepare_session_slot(self.layout, spec.arm_id, inputs.tree)
        except StateError as error:
            return AttemptResult(ABORT, "state", str(error))
        attempt_dir.mkdir(parents=True, exist_ok=True)
        started_wall = time.time()
        transcript_path = attempt_dir / "transcript.jsonl"
        stderr = open(attempt_dir / "claude.stderr", "w", encoding="utf-8")  # noqa: SIM115 - closed below
        proc = subprocess.Popen(  # noqa: S603 - argv built by the harness from the shared builder
            list(inputs.argv),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=stderr,
            text=True,
            bufsize=1,
            cwd=inputs.cwd,
            env=dict(inputs.env),
            start_new_session=True,
        )
        lines: queue.Queue[str | None] = queue.Queue()

        def pump() -> None:
            with open(transcript_path, "w", encoding="utf-8") as sink:
                for line in proc.stdout:
                    sink.write(line)
                    sink.flush()
                    lines.put(line.rstrip("\n"))
            lines.put(None)

        reader = threading.Thread(target=pump, daemon=True)
        reader.start()
        all_events: list[dict] = []
        turn_slices: list[tuple[int, list[str], list[dict], float]] = []
        timed_out = False
        quota: dict | None = None
        ended = False
        try:
            for index, turn in enumerate(inputs.turns):
                slot.turn_file.write_text(str(index), encoding="utf-8")
                sent = time.monotonic()
                try:
                    proc.stdin.write(_user_message(turn) + "\n")
                    proc.stdin.flush()
                except (BrokenPipeError, OSError):
                    ended = True
                    break
                turn_lines: list[str] = []
                turn_events: list[dict] = []
                deadline = sent + self.turn_timeout
                got_result = False
                while not got_result:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        timed_out = True
                        break
                    try:
                        line = lines.get(timeout=remaining)
                    except queue.Empty:
                        timed_out = True
                        break
                    if line is None:
                        ended = True
                        break
                    turn_lines.append(line)
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(event, dict):
                        continue
                    turn_events.append(event)
                    all_events.append(event)
                    info = (
                        event.get("rate_limit_info")
                        if event.get("type") == "rate_limit_event"
                        else None
                    )
                    if isinstance(info, dict) and info.get("status") == "rejected":
                        quota = info
                        break
                    got_result = event.get("type") == "result"
                turn_slices.append((index, turn_lines, turn_events, time.monotonic() - sent))
                if timed_out or ended or quota is not None:
                    break
        finally:
            try:
                proc.stdin.close()
            except OSError:
                pass
            if timed_out or quota is not None:
                _end_process_group(proc)
            else:
                try:
                    proc.wait(timeout=_EXIT_GRACE_SECONDS)
                except subprocess.TimeoutExpired:
                    _end_process_group(proc)
            reader.join(timeout=10)
            stderr.close()
        runtime_copy = attempt_dir / "runtime"
        shutil.copytree(slot.runtime, runtime_copy, dirs_exist_ok=True)
        if slot.hook_home.exists():
            shutil.copytree(slot.hook_home, attempt_dir / "hook-home", dirs_exist_ok=True)
        tap_events = _read_jsonl(runtime_copy / "tap.jsonl")
        orphan = _end_orphan_server(tap_events, slot)
        records = [_turn_record(i, tl, te, tap_events, wall) for i, tl, te, wall in turn_slices]
        result = classify(
            spec=spec,
            model=self.model,
            client_version=self.client_version,
            events=all_events,
            turn_records=records,
            tap_events=tap_events,
            scored_turn=inputs.scored_turn,
            timed_out=timed_out,
            quota=quota,
            surface=self.surfaces.get((spec.arm_id, inputs.tree)),
        )
        result.checks.update(
            returncode=proc.returncode,
            orphan_server_ended=orphan,
            user_state_writes=_user_state_writes(started_wall),
        )
        slot.wipe()
        return result


# --------------------------------------------------------------------------
# The retry rule and the run loop.
# --------------------------------------------------------------------------

Executor = Callable[[SessionInputs, Path], AttemptResult]


def run_session(inputs: SessionInputs, session_dir: Path, execute: Executor) -> dict:
    """Run one session to a final outcome under the protocol's retry rule.

    A harness fault is retried with the same inputs, up to the protocol's limit;
    every attempt is logged with its inputs digest. Anything else is final. A
    session paused by a quota keeps its earlier attempts, and the attempt that
    met the quota does not count against the retry limit.
    """

    session_dir.mkdir(parents=True, exist_ok=True)
    log_path = session_dir / "attempts.jsonl"
    attempts = _read_jsonl(log_path)
    digest = inputs.digest()
    for attempt in attempts:
        if attempt["inputs_digest"] != digest:
            raise RunAborted(
                f"session {inputs.planned.session_id}: its inputs changed since attempt {attempt['attempt']}; "
                "a resumed run must keep its inputs"
            )
    while True:
        faults = sum(1 for a in attempts if a["outcome"] == HARNESS_FAULT)
        number = len(attempts) + 1
        result = execute(inputs, session_dir / f"attempt-{number}")
        entry = {"attempt": number, "inputs_digest": digest, **result.as_dict()}
        attempts.append(entry)
        with open(log_path, "a", encoding="utf-8") as log:
            log.write(json.dumps(entry, sort_keys=True) + "\n")
        if result.outcome == QUOTA:
            raise RunPaused(
                f"session {inputs.planned.session_id}: quota spent (resets at {result.resets_at})"
            )
        if result.outcome == ABORT:
            raise RunAborted(f"session {inputs.planned.session_id}: {result.kind}: {result.detail}")
        if result.outcome == HARNESS_FAULT and faults + 1 <= protocol.HARNESS_FAULT_RETRIES:
            continue
        final = {
            "session_id": inputs.planned.session_id,
            "case_id": inputs.planned.case_id,
            "arm": inputs.planned.arm,
            "repeat": inputs.planned.repeat,
            "tree": inputs.tree,
            "inputs_digest": digest,
            "attempts": len(attempts),
            "harness_faults": sum(1 for a in attempts if a["outcome"] == HARNESS_FAULT),
            "outcome": result.outcome,
            "kind": result.kind,
            "answer": result.answer,
            "turns": result.turns,
            "checks": result.checks,
        }
        (session_dir / "session.json").write_text(
            json.dumps(final, indent=2, sort_keys=True), encoding="utf-8"
        )
        return final


def load_session(session_dir: Path) -> dict | None:
    path = session_dir / "session.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
