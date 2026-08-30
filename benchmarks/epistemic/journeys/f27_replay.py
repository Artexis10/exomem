"""The f27 lifecycle-routing replay driver: an authored episode, through a real agent.

f20-f26 measure detectors, end states and delivery on state the harness itself
wrote. f27 asks the question none of them can: given ten turns of ordinary
working language with every store-bearing utterance removed, does a real agent
session leave the durable state an expert session leaves? That question is only
answerable through the client surface the product's users actually have, so this
module drives the installed agent CLI and reads nothing but the vault afterwards.

**The envelope is discovered, never assumed.** :func:`discover_agent_envelope`
locates the CLI and asks it what it is. With none installed the journey refuses
with :class:`AgentEnvelopeNotDiscovered` rather than importing the product
in-process: an in-process fallback would silently turn a client-surface test
into a library test, which is the one substitution this family cannot survive.

**Two arms, each the surface its own users get.** ``hookless`` is the web and
hosted shape — no plugin, built-in tools off, the documented maximal
custom-instructions block appended, prominence ``maximal``. ``hooked`` is the
shipped Claude Code shape — the plugin directory, ``Skill`` enabled, prominence
``balanced``. Both talk to an isolated stdio server over a fresh copy of the
seeded vault, so neither can see the other's writes or a real vault.

**A failed execution is a harness fault, never a product result.** A non-zero
exit, an error-subtype transcript, a not-logged-in result or a malformed
transcript line blocks the arm: no snapshot is projected and both assertions
come back ``blocked`` with the reason. An empty transcript that flowed into a
projection would score as a product that wrote nothing, which is the single most
expensive lie this harness could tell about itself.

**Nothing here reads a clock.** ``taken_at`` is supplied by the caller so an
artifact is reproducible from its inputs; only :func:`main`, which is a command,
may consult one.

Sequence-3 status: the manifest states the amendment receipt's own
acknowledgment status, derived from the working receipts at write time — never
hardcoded, because a remembered status lies the day the receipt changes.
Acknowledged (2026-08-30), a run may back a comparative claim, with f27
declared expected-partial on the current runtime; while a receipt is pending,
a run is evidence about the harness only, and the artifact says so itself.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import MappingProxyType
from typing import Protocol

from membench.trackc.natural_prompt_driver import write_mcp_config
from membench.trackc.witness_join import Transcript, parse_stream_json_transcript

from ..amendments import withheld_family_ids
from ..corpora.lifecycle_replay import CORPUS_ID, ReplayCorpus, replay_corpus, seed_replay_vault
from ..projectors.exomem_vault import VaultProjector
from ..snapshot import EpistemicStateSnapshot, ProjectorMeta

#: ``benchmarks/epistemic/journeys/f27_replay.py`` -> the repository root.
REPO_ROOT = Path(__file__).resolve().parents[3]

FIXTURE_PATH = (
    REPO_ROOT
    / "benchmarks"
    / "epistemic"
    / "fixtures"
    / "sequence3"
    / "f27-lifecycle-routing-replay.yaml"
)
PROMINENCE_DOC = REPO_ROOT / "docs" / "prominence.md"
PLUGIN_DIR = REPO_ROOT / "plugins" / "claude-code"

#: Candidate executables, in preference order.
AGENT_CANDIDATES: tuple[str, ...] = ("claude",)

#: Exactly-named session variables the parent process leaks into a child.
SESSION_VARIABLES: tuple[str, ...] = ("CLAUDECODE", "CLAUDE_PID")

#: And the family of them. A benchmark turn launched from inside an agent
#: session inherits these, and the CLI reads them to decide it is a nested
#: invocation — which changes the very surface the arm is supposed to present.
SESSION_VARIABLE_PREFIXES: tuple[str, ...] = ("CLAUDE_CODE_",)

#: Turn budget. A replay turn is one user utterance; the agent may take several
#: internal steps to serve it, but an unbounded turn can burn a subscription on
#: a loop.
DEFAULT_MAX_TURNS = 12

#: Per-turn wall clock ceiling for the default subprocess runner.
TURN_TIMEOUT_SECONDS = 900.0

#: Options the installed CLI accepts but does not list in ``--help``.
#:
#: Probed as ``claude <option> mcp list``, which parses argv and exits: the
#: parser answers ``error: unknown option '--x'`` for a name it does not know
#: and says nothing for one it does. ``--version`` is NOT a valid probe — it
#: short-circuits argument validation, which is how round 0 came to claim
#: ``--append-system-prompt-file`` was undocumented when it is merely written
#: ``--append-system-prompt[-file]`` in the help and was being lost by the
#: option regex.
#:
#: One entry, and it is the bound D3 names. The tests assert containment in both
#: directions, so an option the driver stops using cannot linger here pretending
#: a probe still justifies it.
UNDOCUMENTED_BUT_ACCEPTED: frozenset[str] = frozenset({"--max-turns"})

#: Recognised in a result payload or on stderr. An unauthenticated run produces
#: a perfectly well-formed empty session, which is why it is checked for by name.
NOT_LOGGED_IN_RE = re.compile(r"not\s+logged\s+in|please\s+run\s+/login", re.IGNORECASE)

#: One hook event on the wire is a ``hook_started``/``hook_response`` pair, so
#: the response is counted and the start is not. Verified against a recorded
#: ``claude 2.1.240 --include-hook-events`` stream, 2026-08-23; the round-0
#: counter keyed on a ``hook_event`` subtype the CLI never emits and therefore
#: returned zero on a transcript carrying three real pairs.
HOOK_RESPONSE_SUBTYPE = "hook_response"

#: The hook whose *firings* are counted beside the state. A firing is a Stop
#: response carrying the product's own reminder; a response with empty output is
#: an invocation that decided there was nothing to say, and counting it would
#: report a runtime that never spoke up as one that spoke up every turn.
CAPTURE_NUDGE_HOOK_EVENT = "Stop"

#: The two tools whose write actions are the family's subject matter.
STRUCTURED_WRITE_TOOLS: tuple[str, ...] = (
    "mcp__exomem__record_memory",
    "mcp__exomem__plan_memory",
)


def is_structured_write(tool: str, action: str) -> bool:
    """Whether ``tool(action=...)`` is a write, by the product's own definition.

    ``_KB_WRITE`` is the regex the capture nudge uses to decide whether a turn
    already filed something. Borrowing it means the report and the nudge cannot
    disagree about what a write is: if the product widens the set, this widens
    with it, and if it narrows, a test here goes red rather than the number
    quietly drifting.
    """

    from exomem._hooks.exomem_capture_nudge import _KB_WRITE

    return bool(_KB_WRITE.search(f"{tool}:{action}"))


FAULT_PROJECTOR = ProjectorMeta(
    name="f27-replay-journey",
    version="1.0.0",
    author="benchmark-harness",
    endpoints_used=(),
    loc=0,
    loc_code=0,
)


class AgentEnvelopeNotDiscovered(RuntimeError):
    """No installed agent CLI; the replay cannot run through a client surface."""


class JourneySetupError(RuntimeError):
    """The arm could not be set up, before any turn was spent."""


class _ProcLike(Protocol):
    returncode: int
    stdout: str
    stderr: str


Runner = Callable[[list[str]], _ProcLike]


@dataclass(frozen=True)
class AgentEnvelope:
    """The installed CLI, as discovered rather than as assumed."""

    executable: Path
    version: str


@dataclass(frozen=True)
class Arm:
    """One client surface, described as its own users receive it."""

    arm_id: str
    prominence: str
    #: Value for ``--tools``. Empty string disables the built-in tools, which is
    #: what a web client without them looks like.
    tools: str
    allowed_tools: str
    uses_plugin: bool
    uses_custom_instructions: bool
    configure_ref: str


ARMS: Mapping[str, Arm] = MappingProxyType(
    {
        "hookless": Arm(
            arm_id="hookless",
            prominence="maximal",
            tools="",
            allowed_tools="mcp__exomem",
            uses_plugin=False,
            uses_custom_instructions=True,
            configure_ref="hookless-maximal",
        ),
        "hooked": Arm(
            arm_id="hooked",
            prominence="balanced",
            tools="Skill",
            allowed_tools="mcp__exomem Skill",
            uses_plugin=True,
            uses_custom_instructions=False,
            configure_ref="hooked-balanced",
        ),
    }
)

ARM_ORDER: tuple[str, ...] = ("hookless", "hooked")


# --------------------------------------------------------------------------
# Envelope discovery.
# --------------------------------------------------------------------------


def discover_agent_envelope(
    *, which: Callable[[str], str | None] = shutil.which
) -> AgentEnvelope:
    """Locate the installed agent CLI and ask it what it is."""

    for name in AGENT_CANDIDATES:
        found = which(name)
        if not found:
            continue
        completed = subprocess.run(  # noqa: S603 - resolved executable, fixed argv
            [found, "--version"], capture_output=True, text=True, timeout=60.0
        )
        if completed.returncode != 0:
            raise AgentEnvelopeNotDiscovered(
                f"{name} --version exited {completed.returncode}; the envelope "
                "cannot identify itself and a run through it would be unattributable"
            )
        return AgentEnvelope(
            executable=Path(found), version=completed.stdout.strip() or "unknown"
        )
    raise AgentEnvelopeNotDiscovered(
        "no agent CLI found on PATH "
        f"(looked for {', '.join(AGENT_CANDIDATES)}); f27 measures a client "
        "surface and has no in-process fallback, because a library call would "
        "answer a different question"
    )


def _long_options(help_text: str) -> frozenset[str]:
    """Every long option a help text declares, bracketed variants expanded.

    The help writes an option pair as ``--append-system-prompt[-file]``. A regex
    that stopped at the bracket saw one option where two are declared, and the
    argv check then reported a documented flag as undocumented.
    """

    options: set[str] = set()
    for stem, optional in re.findall(r"(--[A-Za-z][\w-]*)(\[-[\w-]+\])?", help_text):
        options.add(stem)
        if optional:
            options.add(stem + optional[1:-1])
    return frozenset(options)


def declared_cli_options(envelope: AgentEnvelope) -> frozenset[str]:
    """Every long option the installed CLI's own help declares."""

    completed = subprocess.run(  # noqa: S603 - resolved executable, fixed argv
        [str(envelope.executable), "--help"], capture_output=True, text=True, timeout=60.0
    )
    if completed.returncode != 0:
        raise JourneySetupError(
            f"--help exited {completed.returncode}; the driver's argv cannot be "
            "checked against the envelope that will receive it"
        )
    options = _long_options(completed.stdout)
    if not options:
        # An empty set would make every argv check pass vacuously the day the
        # help format changes, which is the drift this function exists to catch.
        raise JourneySetupError("the envelope's help output declares no options")
    return options


# --------------------------------------------------------------------------
# The environment floor.
# --------------------------------------------------------------------------


def environment_floor(parent: Mapping[str, str]) -> tuple[dict[str, str], tuple[str, ...]]:
    """The parent environment minus the variables that mark a nested session.

    Returns the floor and the names removed, because a run artifact that does
    not say what it stripped cannot be reproduced from itself.
    """

    removed = tuple(
        sorted(
            key
            for key in parent
            if key in SESSION_VARIABLES or key.startswith(SESSION_VARIABLE_PREFIXES)
        )
    )
    floor = {key: value for key, value in parent.items() if key not in removed}
    return floor, removed


def environment_delta(
    parent: Mapping[str, str], child: Mapping[str, str]
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    """``(removed, added, changed)`` between the parent and the child env.

    Reported in full because a filtered delta is what hid the ``HOME`` override
    that made every live turn answer "Not logged in": the round-0 dry run printed
    the ``EXOMEM_*`` keys it was proud of and nothing else.
    """

    removed = tuple(sorted(key for key in parent if key not in child))
    added = tuple(sorted(f"{key}={child[key]}" for key in child if key not in parent))
    changed = tuple(
        sorted(
            f"{key}={parent[key]!r} -> {child[key]!r}"
            for key in child
            if key in parent and parent[key] != child[key]
        )
    )
    return removed, added, changed


def arm_environment(
    floor: Mapping[str, str], *, arm: Arm, workdir: Path, vault: Path
) -> dict[str, str]:
    """The floor with every product variable pinned under the arm's own workdir.

    Inherited ``EXOMEM_*`` variables are dropped wholesale rather than
    overridden one by one: the hooked arm's hooks are child processes of the CLI
    and read this environment, so a stray inherited variable would point them at
    a real vault.

    **``HOME`` is deliberately left alone.** The agent's OAuth credentials live
    under ``$HOME/.claude/``; a run that moved ``HOME`` produced a perfectly
    well-formed 91 ms session answering "Not logged in · Please run /login", and
    that is a harness fault dressed as a product one. ``CLAUDE_CONFIG_DIR`` moves
    the credentials too, so it is not the alternative, and copying credentials
    into a benchmark directory is forbidden. Isolation here is
    ``--setting-sources project``, an out-of-tree cwd, the ``EXOMEM_*`` pins and
    ``EXOMEM_HOOK_HOME`` — not the credential store. One consequence is stated
    rather than hidden: the operator's real ``~/.claude/projects/<cwd>/`` gains a
    transcript directory for the benchmark cwd.
    """

    env = {key: value for key, value in floor.items() if not key.startswith("EXOMEM_")}
    env.update(
        {
            "EXOMEM_VAULT_PATH": str(vault),
            "EXOMEM_CONFIG_PATH": str(workdir / "exomem-config.json"),
            "EXOMEM_WRITER_LEASE_STATE_DIR": str(workdir / "leases"),
            "EXOMEM_LOG_DIR": str(workdir / "logs"),
            "EXOMEM_DISABLE_EMBEDDINGS": "1",
        }
    )
    if arm.uses_plugin:
        env["EXOMEM_HOOK_HOME"] = str(workdir / "hook-home")
    return env


#: An ancestor carrying any of these hands the agent context the family says it
#: never had. Measured 2026-08-23: from a cwd inside this repository, with
#: ``--setting-sources project``, the child's own listing of its memory files
#: named this repository's ``CLAUDE.md`` *and* the operator's
#: ``~/.claude/CLAUDE.md``; from an out-of-tree cwd it named none. Memory-file
#: discovery walks the directory tree and is not governed by
#: ``--setting-sources``.
UNSAFE_ANCESTOR_MARKERS: tuple[str, ...] = ("CLAUDE.md", ".claude", ".git")


def refuse_unsafe_out_dir(out_dir: Path) -> Path:
    """Refuse an ``--out`` whose tree would leak memory files into the arms.

    f27's own scenario says neither arm observes a memory file that names the
    store. This repository's ``CLAUDE.md`` opens by naming it. Running the
    benchmark from inside the checkout would therefore have measured a product
    that had been told the answer, and the two arms would still have agreed with
    each other, so nothing in the results would have looked wrong.
    """

    resolved = Path(out_dir).expanduser().resolve()
    for ancestor in (resolved, *resolved.parents):
        for marker in UNSAFE_ANCESTOR_MARKERS:
            candidate = ancestor / marker
            if candidate.exists():
                raise JourneySetupError(
                    f"--out {resolved} sits under {candidate}: an agent turn run from "
                    "there discovers that memory file, and f27 measures a session that "
                    "was never told the store exists. Choose a path outside every "
                    "repository and outside any directory holding CLAUDE.md or "
                    ".claude/ (a scratch directory such as /tmp/f27-run)"
                )
    return resolved


# --------------------------------------------------------------------------
# The invocation.
# --------------------------------------------------------------------------


def custom_instructions_block(level: str) -> tuple[str, str]:
    """The documented prominence block, read out of the doc that publishes it.

    Returned with the citation it was read from. Restating the block here would
    let the benchmark drift from the instructions users are actually given, and
    the drift would look like a product result.
    """

    lines = PROMINENCE_DOC.read_text(encoding="utf-8").splitlines()
    heading = f"### {level.capitalize()}"
    for index, line in enumerate(lines):
        if not line.startswith(heading):
            continue
        for start in range(index + 1, len(lines)):
            if lines[start].startswith("```"):
                for end in range(start + 1, len(lines)):
                    if lines[end].startswith("```"):
                        citation = (
                            f"{PROMINENCE_DOC.relative_to(REPO_ROOT).as_posix()}:"
                            f"{start + 2}-{end}"
                        )
                        return "\n".join(lines[start + 1 : end]) + "\n", citation
                break
        break
    raise JourneySetupError(
        f"{PROMINENCE_DOC.name} carries no fenced block under {heading!r}; the "
        "arm cannot be configured with the instructions the docs publish"
    )


def build_turn_argv(
    *,
    executable: Path,
    arm: Arm,
    prompt: str,
    mcp_config: Path,
    session_id: str,
    model: str,
    max_turns: int,
    first: bool,
    append_system_prompt_file: Path | None,
    plugin_dir: Path | None,
) -> list[str]:
    """One turn's exact invocation.

    The first turn opens a named session; every later turn resumes it, because
    a family about a multi-turn episode measured over ten fresh sessions would
    be measuring ten first turns.
    """

    if arm.uses_custom_instructions and append_system_prompt_file is None:
        raise JourneySetupError(f"arm {arm.arm_id} needs its custom-instructions file")
    if not arm.uses_custom_instructions and append_system_prompt_file is not None:
        raise JourneySetupError(f"arm {arm.arm_id} takes no custom-instructions file")
    if arm.uses_plugin and plugin_dir is None:
        raise JourneySetupError(f"arm {arm.arm_id} needs its plugin directory")
    if not arm.uses_plugin and plugin_dir is not None:
        raise JourneySetupError(f"arm {arm.arm_id} takes no plugin directory")

    argv = [
        str(executable),
        "-p",
        prompt,
        "--strict-mcp-config",
        "--mcp-config",
        str(mcp_config),
        # Project settings only: the operator's own user settings would put a
        # different surface in front of each arm on each machine.
        "--setting-sources",
        "project",
        "--output-format",
        "stream-json",
        "--verbose",
        "--include-hook-events",
        "--allowedTools",
        arm.allowed_tools,
        "--tools",
        arm.tools,
        "--max-turns",
        str(max_turns),
        "--model",
        model,
    ]
    argv.extend(["--session-id", session_id] if first else ["--resume", session_id])
    if plugin_dir is not None:
        argv.extend(["--plugin-dir", str(plugin_dir)])
    if append_system_prompt_file is not None:
        argv.extend(["--append-system-prompt-file", str(append_system_prompt_file)])
    return argv


# --------------------------------------------------------------------------
# Execution.
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ArmContext:
    """Everything an arm's runner needs, and everything a test needs to fake it."""

    arm: Arm
    workdir: Path
    vault: Path
    mcp_config: Path
    session_id: str
    env: Mapping[str, str]
    #: What `seed_replay_vault` returned: the two collection paths and the parent
    #: plan ref as strings, plus `warnings` as a tuple of captured log lines.
    seeded: Mapping[str, object]


RunnerFactory = Callable[[ArmContext], Runner]
ProminenceWriter = Callable[[Arm, Mapping[str, str]], str]


def _subprocess_runner_factory(ctx: ArmContext) -> Runner:
    def run(argv: list[str]) -> _ProcLike:
        return subprocess.run(  # noqa: S603 - benchmark-owned argv; operator-run path
            argv,
            cwd=str(ctx.workdir),
            env=dict(ctx.env),
            capture_output=True,
            text=True,
            timeout=TURN_TIMEOUT_SECONDS,
        )

    return run


def _write_prominence(arm: Arm, env: Mapping[str, str]) -> str:
    """Set the arm's prominence through the product's own command."""

    completed = subprocess.run(  # noqa: S603 - fixed argv, interpreter-resolved
        [sys.executable, "-m", "exomem", "prominence", arm.prominence],
        env=dict(env),
        capture_output=True,
        text=True,
        timeout=120.0,
    )
    if completed.returncode != 0:
        raise JourneySetupError(
            f"setting prominence {arm.prominence} for arm {arm.arm_id} exited "
            f"{completed.returncode}: {(completed.stderr or completed.stdout).strip()[:300]}"
        )
    return completed.stdout.strip()


def result_subtype(stdout: str) -> str | None:
    """The ``result`` line's subtype, which the parsed Transcript does not carry.

    Read from the raw stream rather than added to track C's ``Transcript``: that
    dataclass is shared with the track C gates, and f27 needing one more field
    is not a reason to change what those parse.
    """

    for raw in reversed(stdout.splitlines()):
        raw = raw.strip()
        if not raw:
            continue
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and payload.get("type") == "result":
            subtype = payload.get("subtype")
            return subtype if isinstance(subtype, str) else None
    return None


def fault_reason(proc: _ProcLike, transcript: Transcript) -> str | None:
    """Why this execution cannot become a product result, or ``None``."""

    if transcript.malformed_lines:
        return (
            f"the transcript carries {transcript.malformed_lines} malformed line(s); "
            "a partially parsed episode cannot be scored as one"
        )
    if NOT_LOGGED_IN_RE.search(transcript.result_text or "") or NOT_LOGGED_IN_RE.search(
        proc.stderr or ""
    ):
        return "the run reports it is not logged in, so no agent turn was served"
    # Checked BEFORE the exit code: a turn cap exits non-zero too, and "exited 1"
    # is the least useful thing that could be said about it.
    if result_subtype(proc.stdout or "") == "error_max_turns":
        # Named, because a turn cap and a crash both arrive as a non-zero exit
        # with an error result and the operator's next move is different: raise
        # --max-turns, or go and read the transcript. Live, one user utterance
        # cost five agent turns on both arms.
        served = transcript.num_turns if transcript.num_turns is not None else "an unknown number of"
        return (
            f"the run hit its turn cap: result subtype error_max_turns after {served} "
            "agent turns; raise --max-turns rather than reading this as a crash"
        )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()[:300]
        return f"the agent exited with exit code {proc.returncode}: {detail or 'no output'}"
    if transcript.is_error:
        return f"the transcript's result is an error: {transcript.result_text.strip()[:300]!r}"
    return None


def _capture_nudge_prefix() -> str:
    """The reminder's opening line, imported from the hook that emits it.

    Imported rather than restated: a literal here could drift from the text the
    product actually writes, and the counter would then report zero firings for
    a runtime that nudged on every turn — indistinguishable, in the report, from
    one that never nudged at all.
    """

    from exomem._hooks.exomem_capture_nudge import REMINDER

    return REMINDER.split(".", 1)[0]


def count_hook_activity(stdout: str) -> tuple[int, int]:
    """``(hook invocations, capture-nudge firings)`` in one transcript."""

    prefix = _capture_nudge_prefix()
    invocations = 0
    firings = 0
    for raw in stdout.splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        if payload.get("type") != "system" or payload.get("subtype") != HOOK_RESPONSE_SUBTYPE:
            continue
        invocations += 1
        output = payload.get("output")
        if (
            payload.get("hook_event") == CAPTURE_NUDGE_HOOK_EVENT
            and isinstance(output, str)
            and prefix in output
        ):
            firings += 1
    return invocations, firings


@dataclass
class ArmResult:
    """One arm's execution: what was run, what came back, and what it left."""

    arm_id: str
    session_id: str
    prominence: str
    argvs: tuple[tuple[str, ...], ...] = ()
    transcripts: tuple[Transcript, ...] = ()
    hook_invocations: int = 0
    capture_nudge_firings: int = 0
    harness_fault: bool = False
    fault_reason: str | None = None
    snapshot: EpistemicStateSnapshot | None = None
    #: The seeded vault as projected before turn 1: the false-write baseline.
    seed_snapshot: EpistemicStateSnapshot | None = None
    #: Anything the seed's own graph-sync logging had to say, captured at the
    #: seam rather than left on stderr where it reads as a crash.
    seed_warnings: tuple[str, ...] = ()
    env_removed: tuple[str, ...] = ()
    citations: tuple[str, ...] = ()

    def tool_uses(self) -> dict[str, int]:
        """Every tool the arm called, by name."""

        counts: dict[str, int] = {}
        for transcript in self.transcripts:
            for use in transcript.tool_uses:
                counts[use.name] = counts.get(use.name, 0) + 1
        return counts

    def structured_write_tool_uses(self) -> dict[str, int]:
        """Only the calls that actually wrote a plan item or a record.

        Live, both arms opened by calling ``plan_memory(action="inspect")`` —
        discovery, not a write. Counting those would have reported a session that
        looked at the collection as one that filed into it, which is the exact
        distinction this family exists to measure.
        """

        counts = {name: 0 for name in STRUCTURED_WRITE_TOOLS}
        for transcript in self.transcripts:
            for use in transcript.tool_uses:
                if use.name not in counts:
                    continue
                action = str((use.input or {}).get("action") or "").strip()
                if action and is_structured_write(use.name, action):
                    counts[use.name] += 1
        return counts

    def usage(self) -> dict[str, int]:
        totals: dict[str, int] = {"input_tokens": 0, "output_tokens": 0}
        for transcript in self.transcripts:
            for key, value in (transcript.usage or {}).items():
                if isinstance(value, int):
                    totals[key] = totals.get(key, 0) + value
        return totals


@dataclass
class JourneyResult:
    """Both arms of one replay, plus everything needed to attribute it."""

    arms: tuple[ArmResult, ...]
    envelope: AgentEnvelope
    model: str
    taken_at: str
    corpus: ReplayCorpus
    out_dir: Path
    dry_run: bool = False
    dry_run_lines: tuple[str, ...] = field(default_factory=tuple)


def _session_id() -> str:
    """A fresh session id per arm per run, recorded in the manifest.

    Deliberately not derived from ``--taken-at``: ``--session-id`` is refused if
    the id already exists, so a rerun pinned to the same timestamp — exactly what
    an operator does when reproducing an artifact — would hand the CLI an id it
    already used. Reproducibility belongs to the corpus digest and the manifest,
    not to a value the CLI treats as unique.
    """

    return str(uuid.uuid4())


def _fault_snapshot(arm: ArmResult, *, taken_at: str) -> EpistemicStateSnapshot:
    """A snapshot that carries the fault instead of an absence of state.

    An arm that never ran left no state, and an empty projection would score as
    a product that wrote nothing. This one projects nothing *and says why*, so
    both assertions block on it and the reason travels with the result.
    """

    return EpistemicStateSnapshot(
        provider="exomem",
        variant="native",
        phase=arm.arm_id,
        taken_at=taken_at,
        # The same projector that took the seed snapshot, when there is one. The
        # runner refuses a scenario row whose snapshots came from different
        # projectors — rightly, since comparing two projections of different
        # provenance is exactly the mistake that rule exists to stop — and an arm
        # that seeded and then faulted has one real projection and one absence.
        projector=arm.seed_snapshot.projector if arm.seed_snapshot else FAULT_PROJECTOR,
        completeness_notes=f"harness fault: {arm.fault_reason}",
    )


def _arm_plan(
    arm: Arm,
    *,
    out_dir: Path,
    parent: Mapping[str, str],
    envelope: AgentEnvelope,
    corpus: ReplayCorpus,
    model: str,
    max_turns: int,
) -> tuple[ArmContext, list[list[str]], str | None, tuple[str, ...]]:
    """Everything an arm would run, computed without writing a byte.

    Split out so the dry run and the real run share one derivation. The round-0
    dry run recomputed nothing but *did* seed two real vaults on its way to
    printing "nothing was executed", which is both untrue and enough to make the
    real run into the same ``--out`` refuse.
    """

    workdir = out_dir / arm.arm_id
    vault = workdir / "vault"
    mcp_config = workdir / "mcp.json"
    block_path = workdir / "custom-instructions.txt" if arm.uses_custom_instructions else None
    floor, _removed = environment_floor(parent)
    env = arm_environment(floor, arm=arm, workdir=workdir, vault=vault)

    citations: list[str] = []
    if arm.uses_custom_instructions:
        _block, citation = custom_instructions_block(arm.prominence)
        citations.append(citation)
    plugin_dir = PLUGIN_DIR if arm.uses_plugin else None
    if plugin_dir is not None:
        citations.append(plugin_dir.relative_to(REPO_ROOT).as_posix())

    session_id = _session_id()
    argvs = [
        build_turn_argv(
            executable=envelope.executable,
            arm=arm,
            prompt=turn.text,
            mcp_config=mcp_config,
            session_id=session_id,
            model=model,
            max_turns=max_turns,
            first=index == 0,
            append_system_prompt_file=block_path,
            plugin_dir=plugin_dir,
        )
        for index, turn in enumerate(corpus.turns)
    ]
    ctx = ArmContext(
        arm=arm,
        workdir=workdir,
        vault=vault,
        mcp_config=mcp_config,
        session_id=session_id,
        env=env,
        seeded={},
    )
    return ctx, argvs, str(block_path) if block_path else None, tuple(citations)


def _dry_run_lines(
    arm: Arm,
    ctx: ArmContext,
    argvs: Sequence[Sequence[str]],
    *,
    corpus: ReplayCorpus,
    parent: Mapping[str, str],
    citations: Sequence[str],
) -> list[str]:
    """What the run would do, in full, having done none of it."""

    removed, added, changed = environment_delta(parent, ctx.env)
    lines = [
        f"arm {arm.arm_id}",
        f"  prominence it would set: {arm.prominence} (via `python -m exomem prominence "
        f"{arm.prominence}` against {ctx.env['EXOMEM_CONFIG_PATH']})",
        f"  cwd it would run from:   {ctx.workdir}",
        f"  vault it would seed:     {ctx.vault}",
        f"  env removed: {', '.join(removed) or 'nothing'}",
        f"  env added:   {', '.join(added) or 'nothing'}",
        f"  env changed: {', '.join(changed) or 'nothing'}",
    ]
    if citations:
        lines.append(f"  surface cited from: {', '.join(citations)}")
    lines.extend(
        f"  {turn.turn_id}: {shlex.join(argv)}"
        for turn, argv in zip(corpus.turns, argvs, strict=True)
    )
    return lines


def run_journey(
    *,
    out_dir: Path,
    taken_at: str,
    arm_ids: Sequence[str] = ARM_ORDER,
    runner_factory: RunnerFactory = _subprocess_runner_factory,
    prominence_writer: ProminenceWriter = _write_prominence,
    envelope: AgentEnvelope | None = None,
    model: str = "sonnet",
    max_turns: int = DEFAULT_MAX_TURNS,
    corpus: ReplayCorpus | None = None,
    parent_env: Mapping[str, str] | None = None,
    dry_run: bool = False,
) -> JourneyResult:
    """Replay the corpus through each arm and project the state it left.

    ``dry_run=True`` derives and prints everything and writes nothing at all —
    no vault, no config, no file under ``--out``. Envelope discovery still runs,
    because a printed argv naming a CLI that is not installed is not a plan.
    """

    unknown = [arm_id for arm_id in arm_ids if arm_id not in ARMS]
    if unknown:
        raise JourneySetupError(f"unknown arm(s): {', '.join(unknown)}")
    corpus = corpus or replay_corpus()
    envelope = envelope or discover_agent_envelope()
    out_dir = refuse_unsafe_out_dir(out_dir)
    parent = dict(os.environ if parent_env is None else parent_env)

    results: list[ArmResult] = []
    lines: list[str] = []
    if dry_run:
        lines.append(
            f"dry run: envelope discovered at {envelope.executable} ({envelope.version}); "
            "nothing below is executed and nothing is written"
        )
    for arm_id in arm_ids:
        arm = ARMS[arm_id]
        ctx, argvs, block_path, citations = _arm_plan(
            arm,
            out_dir=out_dir,
            parent=parent,
            envelope=envelope,
            corpus=corpus,
            model=model,
            max_turns=max_turns,
        )
        removed, _added, _changed = environment_delta(parent, ctx.env)
        result = ArmResult(
            arm_id=arm_id,
            session_id=ctx.session_id,
            prominence=arm.prominence,
            argvs=tuple(tuple(argv) for argv in argvs),
            env_removed=removed,
            citations=citations,
        )

        if dry_run:
            lines.extend(
                _dry_run_lines(
                    arm, ctx, argvs, corpus=corpus, parent=parent, citations=citations
                )
            )
            results.append(result)
            continue

        if ctx.vault.exists():
            # Seeding on top of an existing arm directory surfaces as the
            # product's own CREATE_ONLY_CONFLICT several frames down a
            # collection write, which reads like a product fault and is a
            # harness one. Refuse here, naming the directory.
            raise JourneySetupError(
                f"arm {arm_id} already has a vault at {ctx.vault}; a replay needs a fresh "
                "copy of the seeded vault, so choose a new --out rather than running "
                "a second episode over the first one's state"
            )
        ctx.workdir.mkdir(parents=True, exist_ok=True)
        seeded = seed_replay_vault(ctx.vault)
        result.seed_warnings = tuple(seeded.get("warnings") or ())
        write_mcp_config(
            ctx.mcp_config,
            vault=ctx.vault,
            workdir=ctx.workdir,
            python_executable=sys.executable,
        )
        if block_path is not None:
            block, _citation = custom_instructions_block(arm.prominence)
            Path(block_path).write_text(block, encoding="utf-8")
        ctx = replace(ctx, seeded=seeded)

        # Before turn 1: the baseline the false-write dual diffs against. A page
        # the scaffold laid is not a page the agent wrote, and only an
        # observation can tell the two apart.
        #
        # `phase` is the arm's phase, not the ref name. Both of an arm's
        # snapshots come from the same scenario phase, and the dual refuses a
        # baseline whose phase is not the scored snapshot's — which is how a
        # phase that forgot its own seed op blocks instead of silently being
        # scored against the previous arm's post-run vault. Labelling the seed
        # `<arm>-seed` would have made that check a naming convention.
        result.seed_snapshot = VaultProjector(ctx.vault).project(
            phase=arm_id, taken_at=taken_at
        )
        (ctx.workdir / "seed-snapshot.json").write_text(
            result.seed_snapshot.model_dump_json(indent=2), encoding="utf-8"
        )

        prominence_writer(arm, ctx.env)
        runner = runner_factory(ctx)

        transcripts: list[Transcript] = []
        for turn, argv in zip(corpus.turns, argvs, strict=True):
            proc = runner(list(argv))
            transcript = parse_stream_json_transcript((proc.stdout or "").splitlines())
            reason = fault_reason(proc, transcript)
            (ctx.workdir / f"turn-{turn.turn_id}.jsonl").write_text(
                proc.stdout or "", encoding="utf-8"
            )
            if reason is not None:
                result.harness_fault = True
                result.fault_reason = f"turn {turn.turn_id}: {reason}"
                break
            transcripts.append(transcript)
            invocations, firings = count_hook_activity(proc.stdout or "")
            result.hook_invocations += invocations
            result.capture_nudge_firings += firings

        result.transcripts = tuple(transcripts)
        (ctx.workdir / "argv.json").write_text(
            json.dumps([list(argv) for argv in argvs], indent=2) + "\n", encoding="utf-8"
        )
        if not result.harness_fault:
            result.snapshot = VaultProjector(ctx.vault).project(phase=arm_id, taken_at=taken_at)
            (ctx.workdir / "snapshot.json").write_text(
                result.snapshot.model_dump_json(indent=2), encoding="utf-8"
            )
        results.append(result)

    journey = JourneyResult(
        arms=tuple(results),
        envelope=envelope,
        model=model,
        taken_at=taken_at,
        corpus=corpus,
        out_dir=out_dir,
        dry_run=dry_run,
        dry_run_lines=tuple(lines),
    )
    if not dry_run:
        write_artifacts(journey)
    return journey


# --------------------------------------------------------------------------
# Evaluation and artifacts.
# --------------------------------------------------------------------------


def evaluate_replay(scenario, result: JourneyResult, *, run_root: Path | None = None):
    """Score the scenario from the state the arms left, blocking on a fault."""

    from ..runner import evaluate_scenario

    snapshots: dict[str, EpistemicStateSnapshot] = {}
    for arm in result.arms:
        # The seed snapshot is the phase's first, so `ctx.prior` is the seeded
        # vault when the expectations are evaluated. An arm that faulted before
        # its vault was seeded has neither, and a fault snapshot stands in for
        # both so the reason reaches the evidence either way — as two distinct
        # objects, because `runner._validate_inputs` refuses a row that binds one
        # observation under two refs, and an arm with nothing to show would then
        # raise instead of reporting four blocked assertions.
        snapshots[f"s-{arm.arm_id}-seed"] = (
            arm.seed_snapshot
            if arm.seed_snapshot is not None
            else _fault_snapshot(arm, taken_at=result.taken_at)
        )
        snapshots[f"s-{arm.arm_id}"] = (
            arm.snapshot
            if arm.snapshot is not None
            else _fault_snapshot(arm, taken_at=result.taken_at)
        )
    run = evaluate_scenario(scenario, snapshots=snapshots)
    if run_root is not None:
        Path(run_root).mkdir(parents=True, exist_ok=True)
        (Path(run_root) / "evaluation.json").write_text(
            json.dumps(
                [
                    {
                        "phase": bound.phase_id,
                        "assertion": bound.assertion,
                        "outcome": bound.result.outcome,
                        "evidence": bound.result.evidence,
                    }
                    for bound in run.assertions
                ],
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    return run


def _page_locators(snapshot: EpistemicStateSnapshot | None) -> tuple[str, ...]:
    """Every file page a snapshot projects, in the dual's own terms."""

    from ..assertions import _page_locators as locators

    return tuple(sorted(locators(snapshot)))


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def report_payload(result: JourneyResult) -> dict:
    """The per-arm reading: coverage and its dual, from the same run.

    No total and no score. The tiers are reported separately because summing
    them would let an arm that files no intent look two-thirds right, and the
    false-write count is printed beside coverage because coverage alone rewards
    a product that writes something for every sentence.
    """

    from ..assertions import REPLAY_TIERS, replay_coverage, replay_extras

    arms: list[dict] = []
    for arm in result.arms:
        row: dict = {
            "arm": arm.arm_id,
            "prominence": arm.prominence,
            "turns_executed": len(arm.transcripts),
            "harness_fault": arm.harness_fault,
            "fault_reason": arm.fault_reason,
            # Both numbers, because they answer different questions: how often
            # the runtime's hooks ran at all, and how often the capture check
            # actually spoke. A runtime whose hooks fire every turn and say
            # nothing is not the same finding as one that never fires.
            "hook_invocations": arm.hook_invocations,
            "capture_nudge_firings": arm.capture_nudge_firings,
            "structured_write_tool_uses": arm.structured_write_tool_uses(),
            "tool_uses": arm.tool_uses(),
            "usage": arm.usage(),
            "citations": list(arm.citations),
            "seed_warnings": list(arm.seed_warnings),
        }
        if arm.snapshot is None:
            row["coverage"] = None
            row["extras_count"] = None
            row["extras"] = None
            row["blocked"] = arm.fault_reason
        else:
            coverage = replay_coverage(arm.snapshot, result.corpus.corpus_id)
            extras = replay_extras(
                arm.snapshot, result.corpus.corpus_id, arm.seed_snapshot
            )
            row["coverage"] = (
                coverage
                if isinstance(coverage, str)
                else {
                    tier: {
                        "landed": coverage[tier]["landed"],
                        "expected": coverage[tier]["expected"],
                        "missing": list(coverage[tier]["missing"]),
                    }
                    for tier in REPLAY_TIERS
                }
            )
            row["extras"] = extras if isinstance(extras, str) else list(extras)
            row["extras_count"] = None if isinstance(extras, str) else len(extras)
        arms.append(row)
    return {
        "corpus_id": result.corpus.corpus_id,
        "corpus_digest": result.corpus.digest(),
        "taken_at": result.taken_at,
        "arms": arms,
    }


def manifest_payload(result: JourneyResult) -> dict:
    """What this run was, and what it is not allowed to be cited as."""

    from exomem import __version__ as exomem_version

    return {
        "journey": "f27-lifecycle-routing-replay",
        "corpus_id": CORPUS_ID,
        "corpus_digest": result.corpus.digest(),
        "fixture_path": FIXTURE_PATH.relative_to(REPO_ROOT).as_posix(),
        "fixture_digest": _digest(FIXTURE_PATH),
        "cli_version": result.envelope.version,
        "cli_executable": str(result.envelope.executable),
        "model": result.model,
        "taken_at": result.taken_at,
        "arms": [arm.arm_id for arm in result.arms],
        "prominence": {arm.arm_id: arm.prominence for arm in result.arms},
        "session_ids": {arm.arm_id: arm.session_id for arm in result.arms},
        "environment_removed": {arm.arm_id: list(arm.env_removed) for arm in result.arms},
        # The false-write baseline, by path: what the seeded vault held before
        # turn 1. Recorded so a reader can check the dual's allowlist against an
        # observation instead of taking the assertion's word for it.
        "seeded_pages": {
            arm.arm_id: sorted(_page_locators(arm.seed_snapshot)) for arm in result.arms
        },
        # Normally NON-empty, and that is the fix working rather than a fault:
        # every graph-sync record at WARNING or above during the seed, as
        # "<LEVEL> <logger>: <message>". On this runtime the routine
        # `graph rebuild stopped checkpoint_sha256=... generation=N` ERROR
        # appears in most seeds; it used to reach stderr unattributed. "Clean"
        # means nothing outside that one message class.
        "seed_warnings": {arm.arm_id: list(arm.seed_warnings) for arm in result.arms},
        # Stated rather than hidden: HOME is not moved, because the agent's OAuth
        # credentials live under it, so the operator's real
        # ~/.claude/projects/<cwd>/ gains a transcript directory for each arm's
        # working directory. Nothing is read from it and nothing is copied.
        "agent_transcript_home": str(Path("~/.claude/projects").expanduser()),
        "exomem_version": exomem_version,
        **amendment_status(),
    }


def amendment_status() -> dict:
    """The receipt's own acknowledgment state, derived at artifact-write time.

    Hardcoding this field misstated the ratification status once (caught in
    review, 2026-08-30); the working receipts are the only source. The pending
    arm is not dead code — sequence 2 is pending today, and the next amendment
    will need the same words.
    """

    acknowledged = "f27" not in withheld_family_ids(REPO_ROOT)
    if acknowledged:
        claim_status = (
            "Sequence 3 was acknowledged on 2026-08-30, so a run from this "
            "driver may back a comparative claim. f27 is declared "
            "expected-partial on the current runtime: a red positive is the "
            "family's finding — the next slice's falsification target — not a "
            "harness fault."
        )
    else:
        claim_status = (
            "Development run. The sequence-3 amendment registering f27 is "
            "unacknowledged, so this is evidence about the harness and the "
            "current runtime, recorded as the family's finding, and is not a "
            "comparative claim, score, or ranking."
        )
    return {
        "amendment_sequence": 3,
        "amendment_acknowledged": acknowledged,
        "claim_status": claim_status,
    }


def write_artifacts(result: JourneyResult) -> tuple[Path, Path]:
    """Write ``report.json`` and ``manifest.json`` beside the arms' workdirs."""

    report = result.out_dir / "report.json"
    manifest = result.out_dir / "manifest.json"
    report.write_text(json.dumps(report_payload(result), indent=2) + "\n", encoding="utf-8")
    manifest.write_text(json.dumps(manifest_payload(result), indent=2) + "\n", encoding="utf-8")
    return report, manifest


# --------------------------------------------------------------------------
# Command.
# --------------------------------------------------------------------------


def _now() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def main(
    argv: Sequence[str] | None = None,
    *,
    runner_factory: RunnerFactory = _subprocess_runner_factory,
    prominence_writer: ProminenceWriter = _write_prominence,
) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m epistemic.journeys.f27_replay",
        description=(
            "Replay the f27 lifecycle corpus through the installed agent CLI. "
            "A run costs real subscription turns; --dry-run prints every "
            "invocation and executes nothing."
        ),
    )
    parser.add_argument("--arm", choices=("hookless", "hooked", "both"), default="both")
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--model", default="sonnet")
    parser.add_argument("--max-turns", type=int, default=DEFAULT_MAX_TURNS)
    parser.add_argument("--taken-at", default=None)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(list(argv) if argv is not None else None)

    arm_ids = ARM_ORDER if args.arm == "both" else (args.arm,)
    try:
        result = run_journey(
            out_dir=args.out,
            taken_at=args.taken_at or _now(),
            arm_ids=arm_ids,
            runner_factory=runner_factory,
            prominence_writer=prominence_writer,
            model=args.model,
            max_turns=args.max_turns,
            dry_run=args.dry_run,
        )
    except (AgentEnvelopeNotDiscovered, JourneySetupError) as error:
        print(f"f27 replay refused: {error}", file=sys.stderr)
        return 2

    if args.dry_run:
        for line in result.dry_run_lines:
            print(line)
        print(
            "dry run: no agent turn was executed, no vault was seeded, and nothing "
            f"was written under {args.out}"
        )
        return 0

    for arm in result.arms:
        if arm.harness_fault:
            print(f"arm {arm.arm_id}: HARNESS FAULT — {arm.fault_reason}", file=sys.stderr)
    print(f"report:   {result.out_dir / 'report.json'}")
    print(f"manifest: {result.out_dir / 'manifest.json'}")
    return 1 if any(arm.harness_fault for arm in result.arms) else 0


__all__ = [
    "AGENT_CANDIDATES",
    "ARMS",
    "ARM_ORDER",
    "AgentEnvelope",
    "AgentEnvelopeNotDiscovered",
    "Arm",
    "ArmContext",
    "ArmResult",
    "DEFAULT_MAX_TURNS",
    "FIXTURE_PATH",
    "JourneyResult",
    "JourneySetupError",
    "SESSION_VARIABLES",
    "STRUCTURED_WRITE_TOOLS",
    "SESSION_VARIABLE_PREFIXES",
    "UNDOCUMENTED_BUT_ACCEPTED",
    "arm_environment",
    "build_turn_argv",
    "count_hook_activity",
    "custom_instructions_block",
    "declared_cli_options",
    "discover_agent_envelope",
    "environment_delta",
    "environment_floor",
    "is_structured_write",
    "evaluate_replay",
    "fault_reason",
    "main",
    "manifest_payload",
    "refuse_unsafe_out_dir",
    "result_subtype",
    "report_payload",
    "run_journey",
    "write_artifacts",
]


if __name__ == "__main__":  # pragma: no cover - command entry point
    raise SystemExit(main())
