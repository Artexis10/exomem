"""Sessions: the plan, one headless session, its classification and the retry rule.

A session is one agent CLI process in streaming-input mode: the harness writes
the case's quoted user turns to its stdin one at a time, waits for each turn's
``result`` event, and closes stdin after the scored turn. One process means one
fresh session and one Exomem server process for the whole case (design,
"Session"), behind the tap that enforces the pull budget.

Classification reads structured fields only: the CLI's event types, its
documented error kinds and the tap's log. A crash of the server under test
after its ready gate opens is an actor failure and is never retried; a
rate limit, a transport error, a host fault or a server that never becomes ready is
a harness fault and is retried with the same inputs, up to the protocol's
limit; a spent quota pauses the run (design, "Runs and failures"). A server
that still fails startup or readiness after the retries stops the run: every
later session would share the fault, and a struck case would hide it.
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
from .arms import (
    HOOK_MODE_WORKING_SET,
    ArmSpec,
    canonical_json,
    client_instructions,
    f27_arm,
    hook_settings,
    sha256_text,
)
from .cases import Case
from .state import (
    PRODUCT_ENV_PREFIXES,
    SERVER_NAME,
    Layout,
    Slot,
    StateError,
    mcp_server_entry,
    prepare_session_slot,
    product_env,
    recover_session_custody,
)
from .tap import READY_TIMEOUT_SECONDS, process_start_ticks, ready_wait, warming_refusal

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

#: The fault kind of a server that never answered the client's handshake.
SERVER_START = "server_start"
RECALL_NOT_READY = "recall_not_ready"
#: The fault kind of a client that never reported its session (no init
#: event): a launcher that refused, or a client that died before it started.
CLIENT_START = "client_start"
#: Setup faults: retried like any harness fault, and one that outlasts the
#: retries stops the run, because every later session would meet it too.
_START_FAULTS = frozenset({SERVER_START, CLIENT_START, RECALL_NOT_READY})

#: The hook event of the protocol's push, the only hook an arm wires.
PUSH_HOOK_EVENT = "UserPromptSubmit"

#: The init event's fields recorded with every attempt: what the session's
#: client loaded, so a report can show the surface it ran on.
_INIT_FACTS = (
    "apiKeySource",
    "model",
    "claude_code_version",
    "cwd",
    "permissionMode",
    "output_style",
    "tools",
    "mcp_servers",
    "plugins",
    "plugin_errors",
    "memory_paths",
    "skills",
    "agents",
    "slash_commands",
)

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


#: The client settings that every Claude child of the run carries, actor and judge.
CLIENT_PINS: Mapping[str, str] = {
    # The client's auto memory adds its own instructions and the operator's
    # memory directory to every child; neither the actor nor the judge has them.
    "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
    # The run pins the client binary; a child must not update the operator's
    # installation underneath a run.
    "DISABLE_AUTOUPDATER": "1",
}


def session_env(parent: Mapping[str, str], slot: Slot, spec: ArmSpec) -> dict[str, str]:
    """The child environment: the floor, no inherited product settings, the pins.

    ``KB_*`` are the retrieve hook's legacy names for its own settings; an
    inherited one would change the push. ``PATH`` starts at the build's
    interpreter directory, so the hook's ``python3`` and its CLI rung's
    ``exomem`` are this build. The CLI rung stands in for the deployed service
    transport, which a session must never reach.
    """

    floor, _removed = environment_floor(parent)
    env = {key: value for key, value in floor.items() if not key.startswith(PRODUCT_ENV_PREFIXES)}
    env.update(product_env(slot, spec))
    env["EXOMEM_RETRIEVE_INJECT_CLI"] = "1"
    env.update(CLIENT_PINS)
    # Discovery waits at the tap's ready gate. Let that bounded setup finish
    # before the client gives up connecting (MCP_TIMEOUT is in milliseconds).
    env["MCP_TIMEOUT"] = str(int((READY_TIMEOUT_SECONDS + 60) * 1000))
    build_bin = str(Path(sys.executable).parent)
    rest = [
        entry for entry in floor.get("PATH", "").split(os.pathsep) if entry and entry != build_bin
    ]
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
    turns: tuple[str | dict, ...]
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
            if key.startswith("EXOMEM_") or key in ("PATH", "MCP_TIMEOUT")
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


def launched_argv(launcher: Sequence[str], argv: Sequence[str]) -> list[str]:
    """The command that starts a session: ``argv`` itself, or the launcher
    called as ``LAUNCHER CLIENT -- ARGS``, the exec-wrapper shape of a launcher
    that adds the subscription's credential at exec. The credential then never
    passes through the harness or its records."""

    if not launcher:
        return list(argv)
    return [*launcher, argv[0], "--", *argv[1:]]


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
    #: Pass or fail, each ``True`` or ``False``, with the init facts they read.
    checks: dict = field(default_factory=dict)
    #: What the executor observed around the client, never a pass or a fail.
    facts: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return asdict(self)


def _user_message(turn: str | dict) -> str:
    if isinstance(turn, str):
        turn = {
            "type": "user",
            "message": {"role": "user", "content": turn},
            "parent_tool_use_id": None,
            "session_id": "default",
        }
    return json.dumps(turn)


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
    surface: Mapping | None,
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

    gate_started = any(e.get("event") == "ready_started" for e in tap_events)
    gate = next((e for e in tap_events if e.get("event") == "ready_finished"), None)
    if gate_started and (gate is None or gate.get("outcome") != "ready"):
        return AttemptResult(
            HARNESS_FAULT,
            RECALL_NOT_READY,
            str(gate.get("outcome")) if gate else "server ended during the ready gate",
            turns=list(turn_records),
        )
    if any(
        e.get("event") == "call"
        and e.get("outcome") == "served"
        and warming_refusal({"result": e.get("result"), "error": e.get("error")})
        for e in tap_events
    ):
        return AttemptResult(
            HARNESS_FAULT,
            "recall_warming",
            "a served read refused after the ready gate",
            turns=list(turn_records),
        )

    init = next(
        (e for e in events if e.get("type") == "system" and e.get("subtype") == "init"), None
    )
    if init is not None:
        checks["init"] = {name: init.get(name) for name in _INIT_FACTS if name in init}
        servers = {
            s.get("name"): s.get("status")
            for s in init.get("mcp_servers") or []
            if isinstance(s, dict)
        }
        # What the client loaded beyond the arm: each would reach every
        # session of the run, so the setup is wrong and the run stops.
        loaded = {
            "api_key": init.get("apiKeySource") if init.get("apiKeySource") != "none" else None,
            # The client's bundled plugins (``path`` "builtin") ship with the
            # pinned version and load alike for every arm; any other is not.
            "plugins": [
                plugin
                for plugin in init.get("plugins") or []
                if not (isinstance(plugin, dict) and plugin.get("path") == "builtin")
            ],
            "memory": init.get("memory_paths"),
            "mcp_servers": sorted(set(servers) - {SERVER_NAME}),
        }
        checks.update(api_key_source=init.get("apiKeySource") == "none")
        for name, found in loaded.items():
            if found:
                return AttemptResult(
                    ABORT,
                    f"surface_{name}",
                    f"the client loaded {name} beyond the arm: {found!r}",
                    turns=list(turn_records),
                    checks=checks,
                )
    hooks = [
        e
        for e in events
        if e.get("type") == "system" and e.get("subtype") in ("hook_started", "hook_response")
    ]
    foreign = [e for e in hooks if spec.hook_mode is None or e.get("hook_event") != PUSH_HOOK_EVENT]
    pushes = [record.get("hook_responses") or [] for record in turn_records]
    if foreign or any(len(responses) > 1 for responses in pushes):
        return AttemptResult(
            ABORT,
            "surface_hooks",
            "a hook ran that the arm does not wire: "
            + json.dumps([(e.get("hook_name"), e.get("hook_event")) for e in foreign or hooks]),
            turns=list(turn_records),
            checks=checks,
        )

    initialized = any(e.get("event") == "handshake" for e in tap_events)
    crash = [
        e for e in tap_events if e.get("event") == "server_exit" and e.get("while_client_connected")
    ]
    if crash and initialized:
        return AttemptResult(
            ACTOR_FAILURE,
            "server_crash",
            f"server exited {crash[0].get('returncode')}",
            turns=list(turn_records),
            checks=checks,
        )
    if crash:
        return AttemptResult(
            HARNESS_FAULT,
            SERVER_START,
            f"server exited {crash[0].get('returncode')} before it answered the handshake",
            turns=list(turn_records),
            checks=checks,
        )

    for event in events:
        if event.get("type") == "assistant" and event.get("error"):
            kind = str(event["error"])
            if kind in _ABORT_ERRORS:
                return AttemptResult(
                    ABORT,
                    kind,
                    "the CLI refused the session's setup",
                    turns=list(turn_records),
                    checks=checks,
                )
            if kind in _FAULT_ERRORS:
                return AttemptResult(
                    HARNESS_FAULT,
                    _FAULT_ERRORS[kind],
                    kind,
                    turns=list(turn_records),
                    checks=checks,
                )

    if init is None:
        return AttemptResult(
            HARNESS_FAULT,
            CLIENT_START,
            "the client never reported its session",
            turns=list(turn_records),
            checks=checks,
        )
    expected_tools = sorted(f"mcp__{SERVER_NAME}__{tool}" for tool in spec.tools)
    checks.update(
        tools=sorted(init.get("tools") or []) == expected_tools,
        model=init.get("model") == model,
        client_version=str(init.get("claude_code_version")) == client_version,
        server_connected=servers.get(SERVER_NAME) == "connected",
    )
    if not checks["server_connected"]:
        started = any(e.get("event") == "server_started" for e in tap_events)
        return AttemptResult(
            HARNESS_FAULT,
            SERVER_START,
            f"server status {servers.get(SERVER_NAME)!r}"
            + ("" if started else "; the tap never started the server"),
            turns=list(turn_records),
            checks=checks,
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
        handshake = next((e for e in tap_events if e.get("event") == "handshake"), None)
        listing = next((e for e in tap_events if e.get("event") == "tools_list"), None)
        # A connected server whose answers the tap did not see: the client
        # speaks a handshake the tap does not know, and no session's surface
        # could be checked.
        unseen = [
            name
            for name, seen in (("handshake", handshake), ("tools_list", listing))
            if seen is None
        ]
        if unseen:
            return AttemptResult(
                ABORT,
                "surface_unseen",
                f"the tap saw no {' or '.join(unseen)} answer from a connected server",
                turns=list(turn_records),
                checks=checks,
            )
        # The state stage captured each handshake; compare the one this client sent.
        method = handshake.get("method")
        tools = sorted(listing.get("tools") or [], key=lambda tool: tool.get("name", ""))
        seen = {
            "server_instructions": sha256_text(handshake.get("instructions") or ""),
            "tool_descriptions": sha256_text(canonical_json(tools)),
        }
        for name, digest in seen.items():
            checks[name] = digest == surface[name].get(method)
        for name in ("server_instructions", "tool_descriptions"):
            if checks.get(name) is False:
                return AttemptResult(
                    ABORT,
                    f"surface_{name}",
                    "the server's surface moved after the state stage",
                    checks=checks,
                )

    if spec.hook_mode is not None:
        fallback = _fallback_push() if spec.hook_mode == HOOK_MODE_WORKING_SET else None
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
    times: Sequence[float],
    tap_events: Sequence[dict],
    wall: float,
    setup_wait: float = 0.0,
) -> dict:
    """One turn's facts. ``times`` holds each event's arrival, in seconds after
    the turn was sent; a push's ``seconds`` is its response's arrival minus its
    start's, so it bounds the hook's own wall time from above."""

    transcript = parse_stream_json_transcript(lines)
    result = next((e for e in reversed(events) if e.get("type") == "result"), None)
    started = {
        e.get("hook_id"): t
        for e, t in zip(events, times, strict=True)
        if e.get("type") == "system" and e.get("subtype") == "hook_started"
    }
    hooks = [
        {
            "exit_code": e.get("exit_code"),
            "outcome": e.get("outcome"),
            "push": hook_push(e),
            "seconds": round(t - started[e.get("hook_id")], 3)
            if e.get("hook_id") in started
            else None,
        }
        for e, t in zip(events, times, strict=True)
        if e.get("type") == "system"
        and e.get("subtype") == "hook_response"
        and e.get("hook_event") == PUSH_HOOK_EVENT
    ]
    calls = [e for e in tap_events if e.get("event") == "call" and e.get("turn") == index]
    usage = transcript.usage or {}
    return {
        "index": index,
        "wall_seconds": round(max(0.0, wall - setup_wait), 3),
        "ready_wait_seconds": round(setup_wait, 3),
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
        "activation_calls": sum(1 for c in calls if c.get("name") == protocol.ACTIVATION_TOOL),
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


def _end_orphan_server(tap_events: Sequence[dict]) -> bool:
    """End the session's own server if it outlived the CLI; never any other process."""

    started = next((e for e in tap_events if e.get("event") == "server_started"), None)
    if started is None:
        return False
    pid = int(started["pid"])
    # The start time, not the environment: the server inherits the client's
    # environment, the subscription's delivered credential included.
    ticks = started.get("start_ticks")
    if ticks is None or process_start_ticks(pid) != ticks:
        return False
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return False
    # Do not return the vault while a killed server can still write to it.
    deadline = time.monotonic() + 10
    while process_start_ticks(pid) == ticks:
        try:
            if Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] == "Z":
                break
        except FileNotFoundError:
            break
        if time.monotonic() >= deadline:
            raise StateError(f"session server {pid} did not stop; preserved the vault in its slot")
        time.sleep(0.05)
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
    """Runs one attempt in a prepared slot, or checks out a fresh state copy."""

    layout: Layout
    specs: Mapping[str, ArmSpec]
    model: str
    client_version: str
    surfaces: Mapping[tuple[str, str], Mapping]
    launcher: tuple[str, ...] = ()
    turn_timeout: float = protocol.TURN_TIMEOUT_SECONDS

    def __call__(self, inputs: SessionInputs, attempt_dir: Path) -> AttemptResult:
        spec = self.specs[inputs.planned.arm]
        try:
            slot = prepare_session_slot(
                self.layout, spec.arm_id, inputs.tree, inputs.planned.session_id
            )
            try:
                result = self.execute_prepared(inputs, attempt_dir, slot)
            finally:
                _end_orphan_server(_read_jsonl(slot.runtime / "tap.jsonl"))
                recovery = recover_session_custody(self.layout)
                slot.wipe()
            result.facts["vault_digest_after"] = recovery.digest
            result.facts["receipts_filed"] = recovery.receipts_filed
            return result
        except StateError as error:
            return AttemptResult(ABORT, "state", str(error))

    def execute_prepared(self, inputs: SessionInputs, attempt_dir: Path, slot: Slot) -> AttemptResult:
        """Run trusted prepared inputs in the supplied slot without vault custody."""

        spec = self.specs[inputs.planned.arm]
        attempt_dir.mkdir(parents=True, exist_ok=True)
        started_wall = time.time()
        transcript_path = attempt_dir / "transcript.jsonl"
        stderr = open(attempt_dir / "claude.stderr", "w", encoding="utf-8")  # noqa: SIM115 - closed below
        try:
            proc = subprocess.Popen(  # noqa: S603 - argv built by the harness from the shared builder
                launched_argv(self.launcher, inputs.argv),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=stderr,
                text=True,
                bufsize=1,
                cwd=inputs.cwd,
                env=dict(inputs.env),
                start_new_session=True,
            )
        except BaseException:
            stderr.close()
            raise
        # Each line with the time it arrived: the hook events carry no timing,
        # and the push's wall time is the gap between its start and response.
        lines: queue.Queue[tuple[float, str] | None] = queue.Queue()

        def pump() -> None:
            with open(transcript_path, "w", encoding="utf-8") as sink:
                for line in proc.stdout:
                    sink.write(line)
                    sink.flush()
                    lines.put((time.monotonic(), line.rstrip("\n")))
            lines.put(None)

        reader = threading.Thread(target=pump, daemon=True)
        reader.start()
        all_events: list[dict] = []
        turn_slices: list[tuple[int, list[str], list[dict], list[float], float, float]] = []
        timed_out = False
        quota: dict | None = None
        ended = False
        gate_events: list[dict] = []
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
                turn_times: list[float] = []
                got_result = False
                while not got_result:
                    now = time.monotonic()
                    if not any(e.get("event") == "ready_finished" for e in gate_events):
                        gate_events = _read_jsonl(slot.runtime / "tap.jsonl")
                    remaining = self.turn_timeout - (
                        now - sent - ready_wait(gate_events, sent, now)
                    )
                    if remaining <= 0:
                        timed_out = True
                        break
                    try:
                        # The tap logs the gate independently of CLI output.
                        # Recheck its clock even while the client is silent.
                        item = lines.get(timeout=min(remaining, 1.0))
                    except queue.Empty:
                        continue
                    if item is None:
                        ended = True
                        break
                    received, line = item
                    turn_lines.append(line)
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(event, dict):
                        continue
                    turn_events.append(event)
                    turn_times.append(received - sent)
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
                turn_slices.append(
                    (index, turn_lines, turn_events, turn_times, sent, time.monotonic())
                )
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
        orphan = _end_orphan_server(tap_events)
        records = [
            _turn_record(i, tl, te, tt, tap_events, end - start, ready_wait(tap_events, start, end))
            for i, tl, te, tt, start, end in turn_slices
        ]
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
        result.facts.update(
            returncode=proc.returncode,
            orphan_server_ended=orphan,
            user_state_writes=_user_state_writes(started_wall),
            ready_gate=next((e for e in tap_events if e.get("event") == "ready_finished"), None),
        )
        if result.kind == CLIENT_START:
            # A launcher's refusal reaches only its stderr.
            said = (attempt_dir / "claude.stderr").read_text(encoding="utf-8", errors="replace")
            result.detail += f"; stderr: {said.strip()[-400:] or '(empty)'}"
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
        if result.outcome == HARNESS_FAULT and result.kind in _START_FAULTS:
            raise RunAborted(
                f"session {inputs.planned.session_id}: {result.kind} after "
                f"{protocol.HARNESS_FAULT_RETRIES} retries: {result.detail}"
            )
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
            "facts": result.facts,
        }
        (session_dir / "session.json").write_text(
            json.dumps(final, indent=2, sort_keys=True), encoding="utf-8"
        )
        return final


def load_session(session_dir: Path) -> dict | None:
    path = session_dir / "session.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
