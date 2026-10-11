"""The f33 referent-capture driver: authored turns in one session, the question in a fresh one.

f33 asks whether an agent promotes the durable referents of ordinary
conversation into typed entities and edges without being asked, and whether a
fresh session then reaches them from the person. Each run therefore has two
client sessions:

1. **Capture.** The case's authored turns replay in one ``claude -p`` session,
   as f27 replays its corpus: the first turn opens the session and every later
   turn resumes it. The vault is projected before the first turn and after the
   last one.
2. **Fresh.** A positive case then opens a second, new session with its
   owner-anchored question. The run file records every ``activate_context``
   result this session received, and maps the packet refs to snapshot item ids
   for the "uses" test under the rulings in :data:`USES_RULE`. The hook's
   inject switches are removed from the child, so a packet arrives only through
   ``activate_context``; a hook that still injects memory blocks the run. A twin
   asks no question.

**Account.** Every child runs on the profile named by ``--claude-config-dir``
(``CLAUDE_CONFIG_DIR``), never the default profile. ``HOME`` stays the real
home; the f27 environment floor strips the variables that mark a nested session,
and :data:`CHILD_ENV_REMOVED` strips API credentials.

**Faults.** A harness fault blocks the run and is never scored: a non-zero exit,
an error or non-success result, a login failure, a malformed line, a server
that did not connect, a permission denial, a hook that injected memory, a
missing packet or answer capture, or a blocked assertion. A blocked run is rerun
under the same pins until the case has its scored repeats, at most
``MAX_ATTEMPTS`` times per case per client shape; a case without them is
``incomplete`` (PREREGISTRATION.md §7, sequence 7). A fault no rerun can clear
aborts the whole driver run instead: the wrong profile, an API key, the wrong
plugins, a hook in the hookless shape, or a model or client version that
differs from the first invocation's.

**Claims.** The manifest states the receipt's own acknowledgment status, read at
write time. Until the founder acknowledges sequence 7, every result here is
labelled "pending acknowledgment" and is not a comparative run, score or claim.
The hookless shape is a proxy for a hookless client, never a ChatGPT result.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path
from types import MappingProxyType
from typing import Any

import yaml
from membench.trackc.natural_prompt_driver import write_mcp_config
from membench.trackc.witness_join import parse_stream_json_transcript

from ..amendments import withheld_family_ids
from ..assertions import AssertionContext
from ..projectors.exomem_vault import VaultProjector
from ..registry import resolve
from ..snapshot import EpistemicStateSnapshot
from . import f27_replay as client
from . import referent_capture as cases
from .collection_replay_driver import _environment
from .role_state_replay_driver import _tool_payload

JOURNEY = "f33-referent-capture-replay"
#: The amendment's protocol: 3 scored repeats per case per client shape, and at
#: most 6 attempts to reach them (PREREGISTRATION.md §7, sequence 7).
REPEATS = 3
MAX_ATTEMPTS = 6
ACTIVATE_TOOL = "mcp__exomem__activate_context"
#: The packet's locator fields: `ref` and `path` on every entry, `anchor` in
#: provenance and current state. The packet schema fixes this set
#: (`exomem.working_set.build_packet`); it is not a reading of meaning.
PACKET_REF_KEYS = frozenset({"ref", "path", "anchor"})
#: Ruling of 2026-10-11, fixed before any baseline run: a ref is served when it is
#: a packet anchor, the provenance of a served unit, or a pointer. recent_context
#: never serves, because edit time, not the question, chooses it.
SERVING_FIELDS: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        "anchors": frozenset({"ref", "path"}),
        "units": frozenset({"provenance.path", "provenance.anchor"}),
        "pointers": frozenset({"ref"}),
    }
)
#: Ruling 1b of 2026-10-11: the anchors of a packet that the edit-time prior
#: carried do not serve, for the same reason. The product marks that packet with
#: this `generation.carried_by` value (`exomem.working_set`, the recency carry).
RECENCY_CARRY = "recency"
USES_RULE = {
    "fixed_on": "2026-10-11",
    "fixed_before_any_baseline": True,
    "authority": "the operator's rulings, pending founder acknowledgment of sequence 7",
    "served": (
        "A ref is served when it is a packet anchor, the provenance (path or anchor) of a "
        "served unit, or a pointer, resolved by its part before '#'. recent_context never "
        "serves: edit time, not the question, chooses it."
    ),
    "recency_anchors": (
        "The anchors of a packet whose generation.carried_by is 'recency' do not serve, for "
        "the same reason. Anchors from the turn, focus, conversation or continuity serve."
    ),
    "no_activation": (
        "A complete fresh stream with no activate_context call received an empty packet, so "
        "'uses' fails rather than blocks. An errored call (is_error, or a success:false "
        "envelope) is not a packet: 'uses' is scored on the readable packets, and the run "
        "blocks only when calls were made and none is readable."
    ),
}
#: Removed from every child: the hook's inject switches (current and legacy name),
#: so a packet arrives only through activate_context, and API credentials, so the
#: child runs on the profile's login.
CHILD_ENV_REMOVED: tuple[str, ...] = (
    "EXOMEM_RETRIEVE_INJECT",
    "KB_RETRIEVE_INJECT",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
)
#: The CLI's own marker for a plugin it ships, as its `init` line writes it.
BUILTIN_PLUGIN_PATH = "builtin"
#: The client's documented hook event that adds context before the model answers.
HOOK_PACKET_EVENT = "UserPromptSubmit"


class DriverAbort(client.JourneySetupError):
    """A deterministic setup fault or a pin drift. No rerun fixes one, so the driver stops."""


class _Proc:
    """A finished invocation, or a stand-in for one that never finished."""

    def __init__(self, returncode: int, stdout: str, stderr: str) -> None:
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


Runner = Callable[[list[str], Path, Mapping[str, str]], Any]


def _subprocess_runner(argv: list[str], cwd: Path, env: Mapping[str, str]) -> Any:
    try:
        return subprocess.run(  # noqa: S603 - benchmark-owned argv; operator-run path
            argv,
            cwd=str(cwd),
            env=dict(env),
            capture_output=True,
            text=True,
            timeout=client.TURN_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as error:
        stdout = error.stdout.decode() if isinstance(error.stdout, bytes) else error.stdout
        return _Proc(-1, stdout or "", f"timed out after {client.TURN_TIMEOUT_SECONDS}s")


# --------------------------------------------------------------------------
# Pins.
# --------------------------------------------------------------------------


def verify_case_digests(cases_root: Path) -> dict[str, str]:
    """Every frozen fixture against the receipt, before any turn is spent."""

    recorded = cases.recorded_digests(cases_root)
    for relative in sorted(recorded):
        try:
            data = (Path(cases_root) / relative).read_bytes()
        except OSError as error:
            raise cases.ReferentCaseError(f"f33 fixture cannot be read: {relative}") from error
        cases.verify_fixture_bytes(relative, data, repo_root=cases_root)
    return recorded


def product_identity(product_root: Path) -> dict[str, Any]:
    """The git SHA of the product under test, and whether its code is dirty.

    The product is ``src/`` and ``plugins/``: the server, the hooks and the
    plugin the two client shapes load. ``benchmarks/`` holds the driver, the
    projector and the assertions. A dirty tree has no SHA that names it, so the
    driver refuses one rather than pinning a version it is not.
    """

    def git(*args: str) -> str:
        completed = subprocess.run(  # noqa: S603 - fixed argv
            ["git", "-C", str(product_root), *args], capture_output=True, text=True, timeout=60
        )
        if completed.returncode != 0:
            raise client.JourneySetupError(
                f"git {' '.join(args)} failed in {product_root}: {completed.stderr.strip()[:200]}"
            )
        return completed.stdout.strip()

    dirty = git("status", "--porcelain", "--", "src", "plugins", "benchmarks")
    if dirty:
        raise client.JourneySetupError(
            f"the checkout at {product_root} has uncommitted changes in src/, plugins/ or "
            f"benchmarks/, so no SHA pins it: {dirty.splitlines()[:5]}"
        )
    return {
        "root": str(product_root),
        "head": git("rev-parse", "HEAD"),
        "src_tree": git("rev-parse", "HEAD:src"),
        "plugins_tree": git("rev-parse", "HEAD:plugins"),
        "benchmarks_tree": git("rev-parse", "HEAD:benchmarks"),
    }


def _require_in_process_product(product_root: Path) -> None:
    """The seed and the projection must use the product the server runs."""

    import exomem

    loaded = Path(exomem.__file__).resolve()
    if not loaded.is_relative_to((product_root / "src").resolve()):
        raise client.JourneySetupError(
            f"this interpreter imports exomem from {loaded}, not from {product_root}/src; "
            "put the product's src first on PYTHONPATH so the seed, the projection and "
            "the server run one product version"
        )


# --------------------------------------------------------------------------
# Reading a session.
# --------------------------------------------------------------------------


def _lines(stdout: str) -> Iterable[dict[str, Any]]:
    for raw in stdout.splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            yield payload


def _init_lines(stdout: str) -> list[dict[str, Any]]:
    return [p for p in _lines(stdout) if p.get("type") == "system" and p.get("subtype") == "init"]


def _result_line(stdout: str) -> dict[str, Any]:
    results = [p for p in _lines(stdout) if p.get("type") == "result"]
    return results[-1] if results else {}


def surface_mismatch(
    stdout: str, *, arm: client.Arm, plugin_dir: Path, profile: Path
) -> str | None:
    """A deterministic setup fault: the wrong profile, an API key, the wrong plugins
    or a hook in the hookless shape. The same setup gives the same fault, so it
    aborts the driver run instead of spending reruns on it."""

    for init in _init_lines(stdout):
        auto = str((init.get("memory_paths") or {}).get("auto") or "")
        if not auto or not Path(auto).is_relative_to(profile):
            return f"the session ran outside the pinned profile {profile}: memory path {auto!r}"
        if init.get("apiKeySource") != "none":
            return f"the session used API key source {init.get('apiKeySource')!r}, not the login"
        loaded = {
            str(p.get("path"))
            for p in init.get("plugins") or ()
            if p.get("path") != BUILTIN_PLUGIN_PATH
        }
        expected = {str(plugin_dir)} if arm.uses_plugin else set()
        if loaded != expected:
            return f"the {arm.arm_id} shape loaded plugins {sorted(loaded)}, not {sorted(expected)}"
    if not arm.uses_plugin and any(
        p.get("type") == "system" and str(p.get("subtype", "")).startswith("hook_")
        for p in _lines(stdout)
    ):
        return "a hook ran in the hookless shape, so the profile's own hooks reached it"
    return None


def pin_drift(stdout: str, pins: dict[str, Any]) -> str | None:
    """The first invocation's model and client version pin every later one."""

    for init in _init_lines(stdout):
        seen = {"model": init.get("model"), "claude_code_version": init.get("claude_code_version")}
        if not pins:
            pins.update(seen)
        for key, value in seen.items():
            if value != pins[key]:
                return f"the {key} changed from {pins[key]!r} (first invocation) to {value!r}"
    return None


def session_fault(stdout: str) -> str | None:
    """Why this session cannot be scored, though a rerun may serve, or ``None``.

    Checked after :func:`f27_replay.fault_reason`, which owns exit codes, login
    failures, error results and malformed lines.
    """

    from exomem._hooks.exomem_retrieve_nudge import _STUB_HEADER, _WORKING_SET_HEADER

    if client.result_subtype(stdout) != "success":
        return "the client stream has no successful terminal result"
    inits = _init_lines(stdout)
    if not inits:
        return "the client stream has no init line, so its account and surface are unknown"
    for init in inits:
        servers = {s.get("name"): s.get("status") for s in init.get("mcp_servers") or ()}
        if servers.get("exomem") != "connected":
            return f"the exomem server is not connected: {servers.get('exomem')!r}"
    if any(
        header in context
        for context in _hook_contexts(stdout)
        for header in (_WORKING_SET_HEADER, _STUB_HEADER)
    ):
        return "a hook injected retrieved memory; packets must come only from activate_context"
    denials = _result_line(stdout).get("permission_denials") or []
    if denials:
        return f"the client denied {len(denials)} tool call(s), so the surface differs from the shape"
    return None


def tool_calls(stdout: str) -> list[dict[str, Any]]:
    """Every tool call in one session, paired with its result block when one came back."""

    calls: dict[str, dict[str, Any]] = {}
    ordered: list[dict[str, Any]] = []
    for payload in _lines(stdout):
        blocks = (payload.get("message") or {}).get("content")
        if not isinstance(blocks, list):
            continue
        for block in blocks:
            if not isinstance(block, dict):
                continue
            if payload.get("type") == "assistant" and block.get("type") == "tool_use":
                call = {
                    "id": block.get("id"),
                    "name": block.get("name"),
                    "input": block.get("input"),
                    "result": None,
                    "is_error": None,
                }
                calls[str(block.get("id"))] = call
                ordered.append(call)
            elif payload.get("type") == "user" and block.get("type") == "tool_result":
                call = calls.get(str(block.get("tool_use_id")))
                if call is not None:
                    call["result"] = block.get("content")
                    call["is_error"] = block.get("is_error") is True
    return ordered


def _hook_contexts(stdout: str) -> list[str]:
    """What each ``UserPromptSubmit`` hook added to the fresh session's context."""

    contexts: list[str] = []
    for payload in _lines(stdout):
        if payload.get("subtype") != "hook_response" or payload.get("hook_event") != HOOK_PACKET_EVENT:
            continue
        output = str(payload.get("output") or "")
        try:
            parsed = json.loads(output)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            output = str((parsed.get("hookSpecificOutput") or {}).get("additionalContext") or "")
        if output.strip():
            contexts.append(output)
    return contexts


def packet_captures(stdout: str) -> tuple[list[dict[str, Any]], str | None]:
    """Every ``activate_context`` result of the fresh session, and why none is readable.

    No call at all is an empty packet that serves nothing (ruling 2). An errored
    call, flagged ``is_error`` or answered with the product's ``success: false``
    envelope, is recorded and not scored. "Uses" is scored on the readable
    packets, and the run blocks only when calls were made and none is readable.
    """

    captured: list[dict[str, Any]] = []
    for call in tool_calls(stdout):
        if call["name"] != ACTIVATE_TOOL:
            continue
        payload = _tool_payload(call["result"]) if call["result"] is not None else None
        # Every packet carries `anchors` (`working_set.build_packet`); an error envelope does not.
        readable = not call["is_error"] and isinstance(payload, dict) and "anchors" in payload
        captured.append(
            {
                "tool_use_id": call["id"],
                "readable": readable,
                "packet": payload if readable else None,
                "error": None if readable else (payload or call["result"]),
            }
        )
    if captured and not any(capture["readable"] for capture in captured):
        return captured, (
            f"none of the fresh session's {len(captured)} activate_context call(s) "
            "returned a readable packet"
        )
    return captured, None


def _packet_refs(value: Any, path: tuple[str, ...] = ()) -> Iterable[tuple[str, str]]:
    """``(field, ref)`` for every locator in one packet section, e.g. ``provenance.path``."""

    if isinstance(value, dict):
        for key, child in value.items():
            if key in PACKET_REF_KEYS and isinstance(child, str) and child:
                yield ".".join((*path, key)), child
            else:
                yield from _packet_refs(child, (*path, key))
    elif isinstance(value, list):
        for child in value:
            yield from _packet_refs(child, path)


def served_refs(
    captures: Sequence[Mapping[str, Any]], snapshot: EpistemicStateSnapshot
) -> list[dict[str, Any]]:
    """Every ref a packet carried: the snapshot item it names, and whether it serves.

    The assertion matches item ids, locators and ``exomem_id`` exactly, so a
    memory ref is resolved to its ``exomem_id`` through the product's own
    parser, and a path to the item whose locator or id it is. Every ref is
    recorded, so a run file shows what the ruling left out as well as what it
    counted; only ``serves`` refs reach the "uses" test.
    """

    from exomem.memory_refs import normalize_id, parse_memory_ref

    by_id = {
        normalize_id(item.raw.get("exomem_id")): item.id
        for item in snapshot.items
        if normalize_id(item.raw.get("exomem_id"))
    }
    by_path = {item.locator: item.id for item in snapshot.items if item.locator}
    by_path.update({item.id: item.id for item in snapshot.items})

    def item_for(ref: str) -> str | None:
        # A unit ref (`<memory ref>#<anchor>`) and a state ref (`<anchor>#current`)
        # name a part of the page that comes before '#'.
        page = ref.split("#", 1)[0]
        memory_id = parse_memory_ref(page)
        return by_id.get(memory_id) if memory_id else by_path.get(page)

    out: list[dict[str, Any]] = []
    for capture in captures:
        if not capture["readable"]:
            continue
        packet = capture["packet"]
        recency = (packet.get("generation") or {}).get("carried_by") == RECENCY_CARRY
        for section, value in packet.items():
            for field, ref in _packet_refs(value):
                serves = field in SERVING_FIELDS.get(section, ()) and not (
                    recency and section == "anchors"
                )
                out.append(
                    {"section": section, "field": field, "ref": ref, "item_id": item_for(ref),
                     "serves": serves}
                )
    return out


# --------------------------------------------------------------------------
# One run.
# --------------------------------------------------------------------------


def _expectations(case_id: str, shape: str, cases_root: Path) -> list[str]:
    relative = cases.case_path(case_id)
    data = (cases_root / relative).read_bytes()
    cases.verify_fixture_bytes(relative, data, repo_root=cases_root)
    phase = next(p for p in yaml.safe_load(data)["phases"] if p["phase_id"] == shape)
    return [expectation["assert"] for expectation in phase["expect"]]


def _projector(vault: Path) -> VaultProjector:
    # Runtime surfaces: a twin proves no notice on any declared surface, and the
    # file-only build leaves three of four unprojected. Entity graph: types and
    # typed edges for the positives.
    return VaultProjector(vault, runtime_surfaces=True, entity_graph=True)


def _invoke(
    runner: Runner, argv: list[str], cwd: Path, env: Mapping[str, str], clock: Callable[[], float]
) -> tuple[Any, float]:
    started = clock()
    proc = runner(argv, cwd, env)
    return proc, round(clock() - started, 3)


def _session_record(session_id: str, cwd: Path) -> dict[str, Any]:
    return {"session_id": session_id, "cwd": str(cwd), "invocations": []}


def _record_invocation(
    session: dict[str, Any], label: str, argv: list[str], proc: Any, wall_s: float
) -> None:
    result = _result_line(proc.stdout or "")
    init = (_init_lines(proc.stdout or "") or [{}])[0]
    session["invocations"].append(
        {
            "label": label,
            "argv": argv,
            "exit_code": proc.returncode,
            "wall_s": wall_s,
            "num_turns": result.get("num_turns"),
            "usage": result.get("usage"),
            "total_cost_usd": result.get("total_cost_usd"),
            "model": init.get("model"),
            "claude_code_version": init.get("claude_code_version"),
            "stream": proc.stdout or "",
            "stderr": proc.stderr or "",
        }
    )


def _call_rows(session: str, turn: str, stdout: str) -> list[dict[str, Any]]:
    """The run file's tool-call list. Results stay in the stream, and the packet is parsed apart."""

    return [
        {"session": session, "turn": turn, **{k: v for k, v in call.items() if k != "result"}}
        for call in tool_calls(stdout)
    ]


def _verdict(
    proc: Any, *, arm: client.Arm, profile: Path, observed: dict[str, Any]
) -> tuple[str, str] | None:
    """``("aborted", why)`` for a deterministic fault or a pin drift, ``("blocked", why)``
    for a fault a rerun may clear, or ``None``."""

    stdout = proc.stdout or ""
    abort = pin_drift(stdout, observed) or surface_mismatch(
        stdout, arm=arm, plugin_dir=client.PLUGIN_DIR, profile=profile
    )
    if abort is not None:
        return "aborted", abort
    transcript = parse_stream_json_transcript(stdout.splitlines())
    fault = client.fault_reason(proc, transcript) or session_fault(stdout)
    return ("blocked", fault) if fault is not None else None


def repeat_outcome(polarity: str, assertions: Mapping[str, Mapping[str, str]]) -> str:
    """``pass``, ``fail`` or ``judge_pending`` for one scored repeat.

    A positive passes when its three assertions pass; the "uses" assertion's
    ``unsupported`` result is the one case the blind judge decides, and no judge
    overturns a deterministic fail. A twin passes when it left no referent.
    """

    outcomes = [row["outcome"] for row in assertions.values()]
    if polarity == "twin":
        return "pass" if outcomes == ["pass"] else "fail"
    if "fail" in outcomes:
        return "fail"
    return "pass" if all(o == "pass" for o in outcomes) else "judge_pending"


def run_once(
    case: cases.ReferentCase,
    shape: str,
    *,
    run_dir: Path,
    attempt: int,
    taken_at: str,
    envelope: client.AgentEnvelope,
    model: str,
    max_turns: int,
    profile: Path,
    cases_root: Path,
    floor: Mapping[str, str],
    observed: dict[str, Any],
    runner: Runner = _subprocess_runner,
    prominence_writer: client.ProminenceWriter = client._write_prominence,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Replay one case in one client shape, then ask its question in a fresh session."""

    arm = client.ARMS[shape]
    vault = run_dir / "vault"
    capture_cwd, fresh_cwd = run_dir / "capture", run_dir / "fresh"
    config = run_dir / "mcp.json"
    block = run_dir / "custom-instructions.txt" if arm.uses_custom_instructions else None
    plugin_dir = client.PLUGIN_DIR if arm.uses_plugin else None
    env = client.arm_environment(floor, arm=arm, workdir=run_dir, vault=vault)
    pythonpath = os.pathsep.join(
        filter(None, [str(client.REPO_ROOT / "src"), floor.get("PYTHONPATH", "")])
    )
    server_pins = {
        "EXOMEM_STATE_ROOT": str(run_dir / "state"),
        "XDG_STATE_HOME": str(run_dir / "xdg-state"),
        "PYTHONPATH": pythonpath,
    }
    for name in CHILD_ENV_REMOVED:
        env.pop(name, None)
    env.update(
        {
            **server_pins,
            "KB_MCP_DISABLE_MEDIA_EXTRACTION": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "CLAUDE_CONFIG_DIR": str(profile),
            # The client version is a pin; an update mid-baseline would change it.
            "DISABLE_AUTOUPDATER": "1",
        }
    )
    record: dict[str, Any] = {
        "case_id": case.case_id,
        "polarity": case.polarity,
        "shape": shape,
        "prominence": arm.prominence,
        "attempt": attempt,
        "status": "blocked",
        "fault_reason": None,
        "sessions": {},
        "tool_calls": [],
        "packet": None,
        "fresh_answer": None,
        "snapshots": {},
        "assertions": {},
        "repeat_outcome": None,
    }

    def blocked(reason: str, status: str = "blocked") -> dict[str, Any]:
        record.update(status=status, fault_reason=reason)
        return record

    for cwd in (capture_cwd, fresh_cwd):
        cwd.mkdir(parents=True)
    try:
        with _environment(env):
            cases.seed_inputs(vault, repo_root=cases_root)
            seed = _projector(vault).project(phase=shape, taken_at=taken_at)
        write_mcp_config(config, vault=vault, workdir=run_dir, python_executable=sys.executable)
        payload = json.loads(config.read_text(encoding="utf-8"))
        payload["mcpServers"]["exomem"]["env"].update(server_pins)
        config.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        if block is not None:
            block.write_text(client.custom_instructions_block(arm.prominence)[0], encoding="utf-8")
        prominence_writer(arm, env)
    except (client.JourneySetupError, OSError, ValueError, subprocess.SubprocessError) as error:
        return blocked(f"setup: {error}")
    (run_dir / "seed-snapshot.json").write_text(seed.model_dump_json(indent=2), encoding="utf-8")
    record["snapshots"]["seed"] = "seed-snapshot.json"

    def argv_for(prompt: str, session_id: str, first: bool) -> list[str]:
        return client.build_turn_argv(
            executable=envelope.executable,
            arm=arm,
            prompt=prompt,
            mcp_config=config,
            session_id=session_id,
            model=model,
            max_turns=max_turns,
            first=first,
            append_system_prompt_file=block,
            plugin_dir=plugin_dir,
        )

    capture_id = str(uuid.uuid4())
    capture = record["sessions"]["capture"] = _session_record(capture_id, capture_cwd)
    for index, (turn_id, text) in enumerate(case.turns):
        argv = argv_for(text, capture_id, index == 0)
        proc, wall_s = _invoke(runner, argv, capture_cwd, env, clock)
        _record_invocation(capture, turn_id, argv, proc, wall_s)
        record["tool_calls"].extend(_call_rows("capture", turn_id, proc.stdout or ""))
        verdict = _verdict(proc, arm=arm, profile=profile, observed=observed)
        if verdict is not None:
            return blocked(f"capture turn {turn_id}: {verdict[1]}", verdict[0])

    try:
        with _environment(env):
            end = _projector(vault).project(phase=shape, taken_at=taken_at)
    except (OSError, ValueError) as error:
        return blocked(f"end snapshot: {error}")
    (run_dir / "end-snapshot.json").write_text(end.model_dump_json(indent=2), encoding="utf-8")
    record["snapshots"]["end"] = "end-snapshot.json"

    served: tuple[str, ...] | None = None
    if case.question is not None:
        fresh_id = str(uuid.uuid4())
        fresh = record["sessions"]["fresh"] = _session_record(fresh_id, fresh_cwd)
        argv = argv_for(case.question, fresh_id, True)
        proc, wall_s = _invoke(runner, argv, fresh_cwd, env, clock)
        _record_invocation(fresh, case.key["question_ref"], argv, proc, wall_s)
        record["tool_calls"].extend(_call_rows("fresh", case.key["question_ref"], proc.stdout or ""))
        verdict = _verdict(proc, arm=arm, profile=profile, observed=observed)
        if verdict is not None:
            return blocked(f"fresh session: {verdict[1]}", verdict[0])
        captures, missing = packet_captures(proc.stdout or "")
        refs = served_refs(captures, end)
        record["packet"] = {"captures": captures, "refs": refs}
        if missing is not None:
            return blocked(f"fresh session: {missing}")
        answer = parse_stream_json_transcript((proc.stdout or "").splitlines()).result_text
        if not answer.strip():
            return blocked("fresh session: the answer was not captured")
        record["fresh_answer"] = answer
        served = tuple(
            dict.fromkeys(ref["item_id"] for ref in refs if ref["serves"] and ref["item_id"])
        )

    for name in _expectations(case.case_id, shape, cases_root):
        context = AssertionContext(
            family=cases.FAMILY_ID,
            snapshot=end,
            prior=seed,
            subject=case.case_id,
            served_items=served,
            fresh_answer=record["fresh_answer"],
        )
        result = resolve(name)(context)
        record["assertions"][name] = {"outcome": result.outcome, "evidence": result.evidence}
    for name, row in record["assertions"].items():
        judged = row["outcome"] == "unsupported" and row["evidence"].startswith("judge_fallback")
        if row["outcome"] == "blocked" or (row["outcome"] == "unsupported" and not judged):
            return blocked(f"assertion {name} is {row['outcome']}: {row['evidence']}")
    record.update(status="scored", repeat_outcome=repeat_outcome(case.polarity, record["assertions"]))
    return record


# --------------------------------------------------------------------------
# Cases, reruns and the report.
# --------------------------------------------------------------------------


def case_status(polarity: str, outcomes: Sequence[str], repeats: int) -> str:
    """The amendment's per-case rule over the scored repeats of one client shape."""

    if len(outcomes) < repeats:
        return "incomplete"
    passes = outcomes.count("pass")
    if polarity == "twin":
        return "pass" if passes == len(outcomes) else "fail"
    need = (2 * repeats + 2) // 3  # 2 of 3, the amendment's positive rule
    if passes >= need:
        return "pass"
    return "judge_pending" if passes + outcomes.count("judge_pending") >= need else "fail"


def _write_run(run_dir: Path, record: Mapping[str, Any]) -> None:
    (run_dir / "run.json").write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")


def run_case_shape(
    case: cases.ReferentCase,
    shape: str,
    *,
    out_dir: Path,
    repeats: int,
    max_attempts: int,
    **run_kwargs: Any,
) -> dict[str, Any]:
    """Run until ``repeats`` scored repeats, rerunning a blocked one under the same pins."""

    runs: list[dict[str, Any]] = []
    outcomes: list[str] = []
    clock = run_kwargs.get("clock", time.monotonic)
    for attempt in range(1, max_attempts + 1):
        if len(outcomes) >= repeats:
            break
        run_dir = out_dir / case.case_id / shape / f"attempt-{attempt:02d}"
        started = clock()
        record = run_once(case, shape, run_dir=run_dir, attempt=attempt, **run_kwargs)
        # The whole run: seeding, projections and every client invocation.
        record["run_wall_s"] = round(clock() - started, 1)
        if record["status"] == "scored":
            outcomes.append(record["repeat_outcome"])
            record["repeat"] = len(outcomes)
        _write_run(run_dir, record)
        runs.append(record)
        if record["status"] == "aborted":
            raise DriverAbort(
                f"{case.case_id} {shape} attempt {attempt}: {record['fault_reason']}"
            )
    return {
        "case_id": case.case_id,
        "polarity": case.polarity,
        "category": case.category,
        "shape": shape,
        "status": case_status(case.polarity, outcomes, repeats),
        "runs": runs,
    }


def _table_row(record: Mapping[str, Any]) -> dict[str, Any]:
    invocations = [
        call for session in record["sessions"].values() for call in session["invocations"]
    ]
    return {
        "attempt": record["attempt"],
        "repeat": record.get("repeat"),
        "status": record["status"],
        "fault_reason": record["fault_reason"],
        "assertions": {name: row["outcome"] for name, row in record["assertions"].items()},
        "repeat_outcome": record["repeat_outcome"],
        "run_wall_s": record["run_wall_s"],
        "client_wall_s": round(sum(call["wall_s"] for call in invocations), 1),
        "invocations": len(invocations),
        "agent_turns": sum(call["num_turns"] or 0 for call in invocations),
        "models": sorted({call["model"] for call in invocations if call["model"]}),
        "client_versions": sorted(
            {call["claude_code_version"] for call in invocations if call["claude_code_version"]}
        ),
    }


def amendment_status(cases_root: Path, *, repeats: int, max_attempts: int) -> dict[str, Any]:
    """The receipt's acknowledgment state, read at write time, and what a run may claim."""

    acknowledged = cases.FAMILY_ID not in withheld_family_ids(cases_root)
    protocol = repeats == REPEATS and max_attempts == MAX_ATTEMPTS
    if not acknowledged:
        claim = (
            "pending acknowledgment: amendment sequence 7 is unacknowledged, so this run is "
            "evidence about the harness and the runtime, not a comparative run, score or claim"
        )
    elif not protocol:
        claim = "development run: its repeats or attempts differ from the amendment's protocol"
    else:
        claim = "protocol run under the acknowledged amendment sequence 7"
    return {
        "amendment_sequence": 7,
        "amendment_acknowledged": acknowledged,
        "protocol_repeats_and_attempts": protocol,
        "claim_status": claim,
    }


def run_driver(
    *,
    out_dir: Path,
    taken_at: str,
    profile: Path,
    case_ids: Sequence[str] = (),
    shapes: Sequence[str] = client.ARM_ORDER,
    repeats: int = REPEATS,
    max_attempts: int = MAX_ATTEMPTS,
    model: str = "sonnet",
    max_turns: int = client.DEFAULT_MAX_TURNS,
    envelope: client.AgentEnvelope | None = None,
    cases_root: Path = cases.REPO_ROOT,
    parent_env: Mapping[str, str] | None = None,
    product: Mapping[str, Any] | None = None,
    **run_kwargs: Any,
) -> dict[str, Any]:
    """Check every pin, then run each case in each shape and write the manifest and report."""

    digests = verify_case_digests(cases_root)
    case_set = {case.case_id: case for case in cases.build_case_set(cases_root)}
    unknown = [case_id for case_id in case_ids if case_id not in case_set]
    if unknown or not set(shapes) <= set(client.ARMS):
        raise client.JourneySetupError(f"unknown case(s) {unknown} or shape(s) {list(shapes)}")
    parent = dict(os.environ if parent_env is None else parent_env)
    if not Path(profile).is_dir():
        raise client.JourneySetupError(f"the client profile {profile} is not a directory")
    default_profile = Path(parent.get("HOME") or Path.home()) / ".claude"
    if Path(profile).resolve() == default_profile.resolve():
        raise client.JourneySetupError(
            f"{profile} is the default client profile, which f33 runs must not use; "
            "pass the profile reserved for these runs"
        )
    product = dict(product) if product is not None else product_identity(client.REPO_ROOT)
    _require_in_process_product(client.REPO_ROOT)
    envelope = envelope or client.discover_agent_envelope()
    out_dir = client.refuse_unsafe_out_dir(out_dir)
    if out_dir.exists():
        raise client.JourneySetupError(f"{out_dir} exists; every driver run needs a fresh --out")
    floor, removed = client.environment_floor(parent)
    out_dir.mkdir(parents=True)
    observed: dict[str, Any] = {}

    manifest = {
        "journey": JOURNEY,
        "family_id": cases.FAMILY_ID,
        "taken_at": taken_at,
        "cli_executable": str(envelope.executable),
        "cli_version": envelope.version,
        "model_requested": model,
        "max_turns_per_invocation": max_turns,
        "product": product,
        "prominence": {shape: client.ARMS[shape].prominence for shape in shapes},
        "plugin_dir": str(client.PLUGIN_DIR),
        "claude_config_dir": str(profile),
        "environment_removed": list(removed),
        "receipt": cases.RECEIPT_PATH,
        "fixture_sha256": digests,
        "repeats": repeats,
        "max_attempts": max_attempts,
        "cases": list(case_ids or case_set),
        "shapes": list(shapes),
        "uses_rule": USES_RULE,
        "hookless_label": (
            "The hookless shape is a proxy for a hookless client such as ChatGPT, "
            "never a ChatGPT result."
        ),
        # Stated rather than hidden: the profile gains one transcript and memory
        # directory per session cwd under its projects/ directory.
        "client_state_written": str(Path(profile) / "projects"),
        **amendment_status(cases_root, repeats=repeats, max_attempts=max_attempts),
    }

    def write_manifest() -> None:
        # The pins the first invocation reported; null until a session has run.
        manifest["model"] = observed.get("model")
        manifest["claude_code_version"] = observed.get("claude_code_version")
        (out_dir / "manifest.json").write_text(
            json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
        )

    write_manifest()
    tables: list[dict[str, Any]] = []
    try:
        for case_id in case_ids or sorted(case_set):
            for shape in shapes:
                result = run_case_shape(
                    case_set[case_id],
                    shape,
                    out_dir=out_dir,
                    repeats=repeats,
                    max_attempts=max_attempts,
                    taken_at=taken_at,
                    envelope=envelope,
                    model=model,
                    max_turns=max_turns,
                    profile=Path(profile),
                    cases_root=cases_root,
                    floor=floor,
                    observed=observed,
                    **run_kwargs,
                )
                tables.append({**result, "runs": [_table_row(run) for run in result["runs"]]})
                write_manifest()
                write_report(out_dir, manifest, tables)
    except DriverAbort as error:
        manifest["aborted"] = str(error)
        write_manifest()
        write_report(out_dir, manifest, tables)
        raise
    return {"manifest": manifest, "cases": tables}


def write_report(out_dir: Path, manifest: Mapping[str, Any], tables: Sequence[Mapping]) -> Path:
    """Per case, per shape, per attempt; no figure aggregates across cases, shapes or arms."""

    report = out_dir / "report.json"
    payload = {
        "claim_status": manifest["claim_status"],
        "hookless_label": manifest["hookless_label"],
        "cases": list(tables),
    }
    report.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return report


def main(argv: Sequence[str] | None = None) -> int:
    from datetime import UTC, datetime

    parser = argparse.ArgumentParser(
        prog="python -m epistemic.journeys.f33_referent_replay",
        description="Run f33 cases through the installed agent CLI. Every run costs real turns.",
    )
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--claude-config-dir", required=True, type=Path)
    parser.add_argument("--case", action="append", default=[], dest="cases")
    parser.add_argument("--shape", choices=(*client.ARM_ORDER, "both"), default="both")
    parser.add_argument("--repeats", type=int, default=REPEATS)
    parser.add_argument("--max-attempts", type=int, default=MAX_ATTEMPTS)
    parser.add_argument("--model", default="sonnet")
    parser.add_argument("--max-turns", type=int, default=client.DEFAULT_MAX_TURNS)
    parser.add_argument("--claude-bin", type=Path, default=None)
    args = parser.parse_args(list(argv) if argv is not None else None)

    try:
        envelope = None
        if args.claude_bin is not None:
            binary = str(args.claude_bin)
            envelope = client.discover_agent_envelope(which=lambda _name: binary)
        result = run_driver(
            out_dir=args.out,
            taken_at=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            profile=args.claude_config_dir.expanduser().resolve(),
            case_ids=args.cases,
            shapes=client.ARM_ORDER if args.shape == "both" else (args.shape,),
            repeats=args.repeats,
            max_attempts=args.max_attempts,
            model=args.model,
            max_turns=args.max_turns,
            envelope=envelope,
        )
    except (client.AgentEnvelopeNotDiscovered, client.JourneySetupError, cases.ReferentCaseError) as error:
        print(f"f33 driver refused or stopped: {error}", file=sys.stderr)
        return 2
    print(result["manifest"]["claim_status"])
    for table in result["cases"]:
        for row in table["runs"]:
            print(
                f"{table['case_id']} {table['shape']} attempt {row['attempt']}: {row['status']} "
                f"{row['repeat_outcome'] or row['fault_reason']} {row['assertions']} "
                f"wall {row['run_wall_s']}s, {row['agent_turns']} agent turns"
            )
        print(f"{table['case_id']} {table['shape']}: case {table['status']}")
    return 1 if any(row["status"] == "blocked" for t in result["cases"] for row in t["runs"]) else 0


if __name__ == "__main__":  # pragma: no cover - command entry point
    raise SystemExit(main())
