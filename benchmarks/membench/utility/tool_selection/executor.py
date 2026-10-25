"""Running one tool-selection session: the shape's files, a seeded slot, the end states.

This composes switch_eval's session machinery rather than copying it: the tap,
the argv builder, the isolation floor, ``classify`` and the retry rule are its
own. Three things differ because this eval's tasks write to the vault. Each
session starts from a fresh copy of its case's seeded vault, the end state is
read after the session instead of requiring the vault to come back unchanged, and
the arms are the server's own tool shapes, so an arm's tool list is probed from
the server rather than fixed in a table.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from epistemic.journeys.f27_replay import REPO_ROOT, AgentEnvelope

from membench.utility.switch_eval.arms import (
    DEFAULT_PROFILE,
    ArmSpec,
    canonical_json,
    sha256_text,
)
from membench.utility.switch_eval.session import (
    ABORT,
    ArmFiles,
    AttemptResult,
    ClaudeSessionExecutor,
    PlannedSession,
    SessionInputs,
    _end_orphan_server,
    _read_jsonl,
    session_argv,
    session_env,
)
from membench.utility.switch_eval.state import (
    SERVER_NAME,
    Layout,
    Slot,
    StateError,
    _ask_surface,
    capture_surface,
    mcp_server_entry,
    tree_digest,
)

from .cases import Case
from .score import compact_calls
from .vault import read_end_state

#: The tool shapes the change defines (design, "Decision"); ``grouped`` is the baseline.
SHAPES: tuple[str, ...] = ("grouped", "flat", "nested")

#: The server-side switch for the tool shape (design, "Decision": one build, the
#: shape chosen by the process environment). A build that does not know it
#: serves the grouped surface for every arm; ``require_distinct_shapes`` catches that.
SHAPE_ENV = "EXOMEM_MCP_TOOL_SHAPE"

#: The tool-search settings the client documents. ``false`` loads every schema
#: up front, so the arms differ in what the model is shown and not in a
#: deferred-loading heuristic; the setting in effect is recorded per session.
TOOL_SEARCH_DEFAULT = "false"

#: Tap calls per task. A guard against a loop, well above what a task needs.
CALL_BUDGET = 30

#: Surfaces are captured once per shape and checked by ``classify`` on every
#: session, keyed by this value in place of a vault tree: the server's
#: instructions and schemas do not depend on the vault (probed on a Records and
#: a Planning vault), and a build where they did would abort the run visibly.
SURFACE_TREE = "all-cases"

TEXTS = Path(__file__).resolve().parent / "texts"


def client_instructions() -> str:
    """The client-side instructions, the same text for every shape.

    They name no tool and no shape; the server's own instructions and tool
    schemas are what the arms differ in.
    """

    return (TEXTS / "actor.md").read_text(encoding="utf-8").strip() + "\n"


def arm_spec(shape: str, tools: Sequence[str]) -> ArmSpec:
    """The switch_eval arm for a shape: no push, no hook, the tools the server lists."""

    return ArmSpec(shape, "anchors", None, tuple(sorted(tools)), ("actor.md",), DEFAULT_PROFILE)


def shape_env(shape: str) -> dict[str, str]:
    return {SHAPE_ENV: shape}


def arm_files(layout: Layout, spec: ArmSpec, *, budget: int = CALL_BUDGET) -> ArmFiles:
    """The shape's MCP config and the shared client instructions, as content."""

    slot = Slot(layout.slot)
    base = layout.arm_dir(spec.arm_id) / "session-inputs"
    entry = mcp_server_entry(
        slot,
        spec,
        log=slot.runtime / "tap.jsonl",
        server_stderr=slot.runtime / "server.stderr",
        # Hold the first call until recall and the search indexes answer, so no arm
        # loses a session because its agent happened to search first.
        warm_search=True,
        budget=budget,
    )
    entry["env"] = {**entry["env"], **shape_env(spec.arm_id)}
    return ArmFiles(
        mcp_config=base / "mcp.json",
        instructions=base / "instructions.md",
        settings=None,
        contents={
            "mcp.json": json.dumps({"mcpServers": {SERVER_NAME: entry}}, indent=2, sort_keys=True),
            "instructions.md": client_instructions(),
        },
    )


def probe_env(parent: Mapping[str, str], slot: Slot, spec: ArmSpec, tool_search: str) -> dict:
    """The child environment of a session or a probe: switch_eval's floor and pins."""

    env = session_env(parent, slot, spec)
    env["ENABLE_TOOL_SEARCH"] = tool_search
    return env


def session_inputs(
    *,
    layout: Layout,
    planned: PlannedSession,
    case: Case,
    spec: ArmSpec,
    envelope: AgentEnvelope,
    model: str,
    parent_env: Mapping[str, str],
    seed_digest: str,
    tool_search: str,
    budget: int = CALL_BUDGET,
) -> SessionInputs:
    files = arm_files(layout, spec, budget=budget)
    # Settings that change a result but live in no file the client reads: hashed
    # with the files, so a resumed run cannot change them unnoticed.
    run_settings = canonical_json(
        {"tool_search": tool_search, "call_budget": budget, "shape": spec.arm_id}
    )
    slot = Slot(layout.slot)
    return SessionInputs(
        planned=planned,
        tree=SURFACE_TREE,
        argv=tuple(session_argv(envelope, spec, files, model=model)),
        env=probe_env(parent_env, slot, spec, tool_search),
        cwd=slot.cwd,
        turns=(case.prompt,),
        scored_turn=0,
        files={**files.contents, "run-settings.json": run_settings},
        state_digests={"seeded_vault": seed_digest},
    )


# --------------------------------------------------------------------------
# Seeding.
# --------------------------------------------------------------------------


def seeding_env(parent: Mapping[str, str], scratch: Slot, spec: ArmSpec) -> dict[str, str]:
    """The environment of a seeding child: every product location pinned in scratch."""

    env = session_env(parent, scratch, spec)
    env["PYTHONPATH"] = str(REPO_ROOT / "benchmarks")
    return env


def seed_case(run: Path, case: Case, *, parent_env: Mapping[str, str], spec: ArmSpec) -> dict:
    """Seed ``case``'s vault once under ``run/seeded/<case>`` and record its digest.

    A second call finds the seed and verifies its digest instead of reseeding.
    """

    target = run / "seeded" / case.case_id
    record = run / "seeded" / f"{case.case_id}.json"
    if record.exists():
        known = json.loads(record.read_text(encoding="utf-8"))
        if tree_digest(target / "vault") != known["digest"]:
            raise StateError(f"the seeded vault of {case.case_id} changed since it was seeded")
        return known
    target.mkdir(parents=True, exist_ok=False)
    with tempfile.TemporaryDirectory(prefix="tool-selection-seed-", dir=run) as scratch_name:
        scratch = Slot(Path(scratch_name))
        for path in (scratch.runtime, scratch.cwd, scratch.hook_home, scratch.state):
            path.mkdir(parents=True)
        completed = subprocess.run(  # noqa: S603 - the harness's own interpreter and module
            [
                sys.executable,
                "-m",
                "membench.utility.tool_selection",
                "seed-vault",
                "--case-json",
                json.dumps(case.as_dict()),
                "--out",
                str(target / "vault"),
            ],
            env=seeding_env(parent_env, scratch, spec),
            capture_output=True,
            text=True,
            check=False,
            timeout=600,
        )
    if completed.returncode != 0:
        raise StateError(f"seeding {case.case_id} failed: {completed.stderr.strip()[-600:]}")
    seeded = {"case_id": case.case_id, "digest": tree_digest(target / "vault")}
    record.write_text(json.dumps(seeded, indent=2, sort_keys=True), encoding="utf-8")
    return seeded


# --------------------------------------------------------------------------
# The shape's surface.
# --------------------------------------------------------------------------


def probe_arm(
    layout: Layout,
    shape: str,
    *,
    vault: Path,
    parent_env: Mapping[str, str],
    tool_search: str,
) -> dict:
    """What the server serves for ``shape``: its tool names and the surface hashes.

    One connection lists every tool (no filter), then the arm's real spec is
    captured so the session's ``classify`` can check the surface it was shown.
    """

    slot = Slot(layout.slot)
    slot.wipe()
    shutil.copytree(vault, slot.vault, symlinks=True)
    for path in (slot.state, slot.runtime, slot.cwd, slot.hook_home):
        path.mkdir(parents=True, exist_ok=True)
    try:
        wild = arm_spec(shape, ("*",))
        env = {**probe_env(parent_env, slot, wild, tool_search), **shape_env(shape)}
        slot.turn_file.write_text("-1", encoding="utf-8")
        out = layout.arm_dir(shape) / "surface"
        out.mkdir(parents=True, exist_ok=True)
        _instructions, tools = _ask_surface(slot, wild, env=env, out=out, method="initialize")
        names = sorted(str(tool["name"]) for tool in tools)
        spec = arm_spec(shape, names)
        hashes = capture_surface(slot, spec, env=env, out=out)
    finally:
        slot.wipe()
    return {
        "shape": shape,
        "tools": names,
        "schema_bytes": len(canonical_json(sorted(tools, key=lambda t: t["name"])).encode("utf-8")),
        "tool_bytes": {
            str(tool["name"]): len(canonical_json(tool).encode("utf-8")) for tool in tools
        },
        "hashes": hashes,
    }


def require_distinct_shapes(probes: Mapping[str, Mapping]) -> None:
    """Refuse a run whose arms serve one surface.

    A build that ignores the shape switch serves the grouped surface under every
    arm, and the comparison would then be the same arm against itself. Two shapes
    may list the same tool names in different argument forms (flat and nested), so
    the surface is each tool's schema hash, not its name.
    """

    seen: dict[tuple[tuple[str, str], ...], str] = {}
    for shape, probe in probes.items():
        tools = tuple(sorted(probe["hashes"]["per_tool"]["initialize"].items()))
        if tools in seen:
            raise StateError(
                f"arms {seen[tools]} and {shape} serve the same tools; "
                f"this build does not apply {SHAPE_ENV}"
            )
        seen[tools] = shape


# --------------------------------------------------------------------------
# Facts and the executor.
# --------------------------------------------------------------------------


def isolation_facts(
    *, init: Mapping, tap_events: Sequence[Mapping], tool_search: str, instructions: str
) -> dict:
    """What the session's client loaded and was shown, for the per-session record."""

    handshake = next((e for e in tap_events if e.get("event") == "handshake"), {})
    listing = next((e for e in tap_events if e.get("event") == "tools_list"), {})
    tools = sorted(listing.get("tools") or [], key=lambda tool: str(tool.get("name")))
    skills = init.get("skills")
    return {
        "claude_code_version": init.get("claude_code_version"),
        "model": init.get("model"),
        "tool_search": tool_search,
        "skills": skills,
        "skills_digest": sha256_text(canonical_json(skills)),
        "plugins": init.get("plugins"),
        "memory_paths": init.get("memory_paths"),
        "client_instructions_digest": sha256_text(instructions),
        "server_instructions_digest": sha256_text(handshake.get("instructions") or ""),
        "tool_names": [str(tool.get("name")) for tool in tools],
        "schema_bytes": len(canonical_json(tools).encode("utf-8")),
    }


def session_facts(
    *,
    result: AttemptResult,
    tap_events: Sequence[Mapping],
    before: Mapping,
    after: Mapping,
    tool_search: str,
    instructions: str,
) -> None:
    """Add the evidence scoring reads to ``result.facts``; scoring needs nothing else."""

    result.facts.update(
        surface=isolation_facts(
            init=result.checks.get("init") or {},
            tap_events=tap_events,
            tool_search=tool_search,
            instructions=instructions,
        ),
        calls=compact_calls(tap_events),
        end_state_before=before,
        end_state_after=after,
    )


@dataclass
class ToolSelectionExecutor(ClaudeSessionExecutor):
    """Runs one attempt for real from a copy of the case's seeded vault."""

    cases: Mapping[str, Case] = field(kw_only=True)
    tool_search: str = field(kw_only=True, default=TOOL_SEARCH_DEFAULT)

    def __call__(self, inputs: SessionInputs, attempt_dir: Path) -> AttemptResult:
        case = self.cases[inputs.planned.case_id]
        slot = Slot(self.layout.slot)
        try:
            self._prepare_slot(slot, case)
            before = read_end_state(slot.vault)
            try:
                result = self._execute(inputs, attempt_dir, slot)
            finally:
                _end_orphan_server(_read_jsonl(slot.runtime / "tap.jsonl"))
            after = read_end_state(slot.vault)
            session_facts(
                result=result,
                tap_events=_read_jsonl(attempt_dir / "runtime" / "tap.jsonl"),
                before=before,
                after=after,
                tool_search=self.tool_search,
                instructions=inputs.files["instructions.md"],
            )
            return result
        except StateError as error:
            return AttemptResult(ABORT, "state", str(error))
        finally:
            slot.wipe()

    def _prepare_slot(self, slot: Slot, case: Case) -> None:
        slot.wipe()
        for path in (slot.state, slot.runtime, slot.cwd, slot.hook_home):
            path.mkdir(parents=True)
        seeded = self.layout.run / "seeded" / case.case_id / "vault"
        shutil.copytree(seeded, slot.vault, symlinks=True)

