"""Arms A, M and P, as the protocol's arms table defines them, with their hashes.

The arms differ only in the server's packet mode, the push at each user turn,
the tool filter and the client instructions (design, "Decision and arms").
Everything else an actor sees is the shipped text and is the same for every
arm: the server instructions and the tool descriptions, which the state stage
captures from the server itself (see :mod:`.state`).

- A runs the server in ``anchors`` mode, pushes what the deployed retrieve hook
  pushes, sees the four read tools and gets the current client instructions,
  read from the doc that publishes them.
- M runs the server in ``maps`` mode, pushes what the same retrieve hook, in the
  same mode, pushes over a ``maps`` server, sees the four read tools and is
  told to read the map and pull before the answer. So A and M differ only in
  the packet and the instruction lines.
- P runs the server in ``anchors`` mode, pushes nothing, sees the read tools
  without ``recall_context`` and shares M's pull instruction without the map
  line.

- N is the development arm of design Amendment 15: no Exomem at all. It has no
  server, no tools, no push and no Exomem instruction line, so its actor
  configuration holds only the actor line. It is not in ``protocol.ARM_IDS``,
  so no frozen protocol digest or arm hash of A, M or P names it, and a run
  prepares it only beside development cases (``__main__.cmd_prepare``).

The hook command is rendered by the product's own installer, from the build's
own hook sources, so the push is the deployed shape and not a copy of it. In
the deployed ``mcp`` hook mode the push is a reminder to activate: A and M
fetch their packet with their own ``recall_context`` call, which the tap
counts against the pull budget and in the turn's calls like any other.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from epistemic.journeys.f27_replay import Arm, custom_instructions_block

from membench.adapters.base import Profile
from membench.adapters.exomem_local import embeddings_profile, lexical_profile

from . import protocol
from .tap import BUDGET_REFUSAL, FILTER_REFUSAL


def deployed_profile(name: str = "deployed") -> Profile:
    """Arm A's "deployed configuration": every retrieval setting at the
    product's default, embeddings, the relevance check, the ranking config and
    the corpus cache included.

    It sets nothing itself. What determinism needs is pinned elsewhere: every
    location the product reads or writes is under the session's own slot
    (``state.product_env``), so a log, a watcher or a cache sees only that
    session's copy, and ``profile_settings`` pins the lexical backend.
    """

    return Profile(name=name, settings={})


#: The run's retrieval profiles: the deployed configuration that the real run
#: measures, and the benchmark adapter's two model-free and embedding profiles
#: under the adapter's names. A closed set this module and the adapter define,
#: recorded with its resolved settings as a run input.
RETRIEVAL_PROFILES = {
    "deployed": deployed_profile,
    "neutral-lexical": lexical_profile,
    "recommended-embeddings": embeddings_profile,
}
DEFAULT_PROFILE = "deployed"


def profile_settings(name: str) -> dict[str, str]:
    """The profile's settings as every server of the run gets them.

    Every profile runs on one lexical backend, FTS5, so two hosts never
    measure different backends. The adapter's profiles pin the python backend,
    which is the lexical store's kill switch: the context catalogue and the
    working set then report ``unsupported``, a state no deployment runs. FTS5,
    not ``auto``, makes a host without it fail the state build loudly.
    """

    settings = dict(RETRIEVAL_PROFILES[name]().settings)
    settings["EXOMEM_LEXICAL_BACKEND"] = "fts5"
    return settings


TEXTS = Path(__file__).resolve().parent / "texts"

#: The retrieve hook modes the installer supports (``install_hook.
#: SUPPORTED_ACTIVATION_MODES``). The mode is a run input shared by A and M.
HOOK_MODE_MCP = "mcp"
HOOK_MODE_WORKING_SET = "working-set"

#: The deployed push. The operator's profile installs bind the retrieve hook
#: with ``--activation-mode mcp``; a run that measures a different deployment
#: says so with ``--hook-mode`` and the arm hash records it.
DEFAULT_HOOK_MODE = HOOK_MODE_MCP


@dataclass(frozen=True)
class ArmSpec:
    arm_id: str
    packet_mode: str
    #: ``None`` for an arm with no push.
    hook_mode: str | None
    tools: tuple[str, ...]
    #: ``("doc", level)`` for the published prominence block, else text files.
    instruction_source: tuple[str, ...]
    #: The retrieval profile, the same on every arm: a run input, carried here
    #: because every server environment of the arm is built from its spec.
    profile: str = DEFAULT_PROFILE
    #: ``False`` for the arm with no Exomem server, state or hook (arm N).
    exomem: bool = True

    def allowed_tools_flag(self, server_name: str) -> str:
        return " ".join(f"mcp__{server_name}__{tool}" for tool in self.tools)


def arm_specs(
    *, hook_mode: str = DEFAULT_HOOK_MODE, profile: str = DEFAULT_PROFILE
) -> Mapping[str, ArmSpec]:
    if hook_mode not in (HOOK_MODE_MCP, HOOK_MODE_WORKING_SET):
        raise ValueError(f"unsupported hook mode: {hook_mode!r}")
    if profile not in RETRIEVAL_PROFILES:
        raise ValueError(f"unknown retrieval profile: {profile!r}")
    p_tools = tuple(tool for tool in protocol.READ_TOOLS if tool != protocol.P_FILTERED_TOOL)
    read, pull, maps = protocol.READ_TOOLS, ("pull.md",), ("pull.md", "map.md")
    return {
        "A": ArmSpec("A", "anchors", hook_mode, read, ("doc", "balanced"), profile),
        "M": ArmSpec("M", "maps", hook_mode, read, maps, profile),
        "P": ArmSpec("P", "anchors", None, p_tools, pull, profile),
        # Amendment 15: the development arm without Exomem. The packet mode is a
        # server setting, and N runs no server.
        "N": ArmSpec("N", "none", None, (), (), profile, exomem=False),
    }


def client_instructions(spec: ArmSpec) -> tuple[str, str]:
    """``(text, source)``: the arm's client instructions and where they come from.

    Every arm ends with the actor line: the protocol's actor is told not to name
    its tools in the answer.
    """

    actor = (TEXTS / "actor.md").read_text(encoding="utf-8").strip()
    if not spec.instruction_source:
        # The current client instructions (docs/prominence.md) are Exomem's alone,
        # so with the Exomem lines removed only the actor line stays.
        return actor + "\n", "switch_eval/texts/actor.md"
    if spec.instruction_source[0] == "doc":
        body, source = custom_instructions_block(spec.instruction_source[1])
    else:
        parts = [
            (TEXTS / name).read_text(encoding="utf-8").strip() for name in spec.instruction_source
        ]
        body = "\n\n".join(parts) + "\n"
        source = ", ".join(f"switch_eval/texts/{name}" for name in spec.instruction_source)
    return body.rstrip("\n") + "\n\n" + actor + "\n", source + ", switch_eval/texts/actor.md"


def hook_settings(spec: ArmSpec, *, hook_home: Path) -> dict | None:
    """The ``--settings`` payload that wires the arm's push, or ``None``.

    Only the retrieve hook on ``UserPromptSubmit``: the protocol's push is what
    arrives at each user turn, and the deployed capture and continuation hooks
    would add pushes the arms table does not list.
    """

    if spec.hook_mode is None:
        return None
    from exomem import install_hook

    item = install_hook._nudge_item(  # noqa: SLF001 - the installer's own rendering of the deployed command
        "exomem_retrieve_nudge.py",
        "exomem-retrieve-nudge.sh",
        "UserPromptSubmit",
        install_hook._HOOK_DIR_SRC,  # noqa: SLF001 - the build's own hook sources, not a deployed copy
        install_hook.DEFAULT_CLIENT,
        hook_home,
        spec.hook_mode,
    )
    return json.loads(install_hook.snippet([item], timeout=item["timeout"]))


def hook_sources_digest() -> str:
    """Digest of the hook sources that render the push, so a format change shows."""

    from exomem import install_hook

    digest = hashlib.sha256()
    for name in (
        "exomem_retrieve_nudge.py",
        "exomem-retrieve-nudge.sh",
        "exomem_local_credentials.py",
    ):
        path = install_hook._HOOK_DIR_SRC / name  # noqa: SLF001 - see hook_settings
        digest.update(name.encode() + b"\0" + path.read_bytes() + b"\0")
    return digest.hexdigest()


def _portable(settings: dict, hook_home: Path) -> dict:
    """The hook settings with the checkout and the slot written as placeholders,
    so the hash names the hook and its mode, not where this run sits."""

    from exomem import install_hook

    text = json.dumps(settings, sort_keys=True)
    text = text.replace(str(install_hook._HOOK_DIR_SRC), "<hook-sources>")  # noqa: SLF001 - see hook_settings
    return json.loads(text.replace(str(hook_home), "<hook-home>"))


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def arm_hashes(spec: ArmSpec, *, hook_home: Path, surface: Mapping[str, str] | None) -> dict:
    """The frozen hashes of everything the arm's actor sees.

    ``surface`` carries the server-instruction and tool-description hashes the
    state stage captured; before that stage runs they are reported as pending,
    never as an empty hash. An arm with no Exomem has no server and no tap, so
    those three components read ``none`` and its hash is complete without a stage.
    """

    text, source = client_instructions(spec)
    settings = hook_settings(spec, hook_home=hook_home)
    hook = (
        {
            "settings": _portable(settings, hook_home),
            "sources": hook_sources_digest(),
            "packet_mode": spec.packet_mode,
        }
        if settings is not None
        else None
    )
    components = {
        "client_instructions": sha256_text(text),
        "hook_output_format": sha256_text(canonical_json(hook)) if hook is not None else "none",
        "tool_filter": sha256_text(canonical_json(sorted(spec.tools))),
        "harness_refusals": sha256_text(BUDGET_REFUSAL + "\n" + FILTER_REFUSAL)
        if spec.exomem
        else "none",
        "packet_mode": spec.packet_mode,
        "server_instructions": (surface or {}).get(
            "server_instructions", "pending: state stage not run" if spec.exomem else "none"
        ),
        "tool_descriptions": (surface or {}).get(
            "tool_descriptions", "pending: state stage not run" if spec.exomem else "none"
        ),
    }
    complete = surface is not None or not spec.exomem
    return {
        "arm": spec.arm_id,
        "components": components,
        "client_instructions_source": source,
        "hook_mode": spec.hook_mode,
        "arm_hash": sha256_text(canonical_json(components))
        if complete
        else "pending: state stage not run",
    }


def f27_arm(spec: ArmSpec, *, server_name: str) -> Arm:
    """The arm as the shared argv builder's :class:`Arm`.

    No built-in tools (the protocol's actor has no web, file or shell tools),
    no plugin (its other hooks are not part of any arm), and the client
    instructions appended as the system prompt file.
    """

    return Arm(
        arm_id=spec.arm_id,
        prominence="balanced",
        tools="",
        allowed_tools=spec.allowed_tools_flag(server_name),
        uses_plugin=False,
        uses_custom_instructions=True,
        configure_ref=f"switch-eval-{spec.arm_id}",
    )
