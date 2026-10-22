"""Arms A, M and P, as the protocol's arms table defines them, with their hashes.

The arms differ only in the server's packet mode, the push at each user turn,
the tool filter and the client instructions (design, "Decision and arms").
Everything else an actor sees is the shipped text and is the same for every
arm: the server instructions and the tool descriptions, which the state stage
captures from the server itself (see :mod:`.state`).

- A runs the server in ``anchors`` mode, pushes what the deployed retrieve hook
  pushes, sees the four read tools and gets the current client instructions,
  read from the doc that publishes them.
- M runs the server in ``maps`` mode, pushes what the retrieve hook pushes in
  working-set mode over a ``maps`` server (the map), sees the four read tools
  and is told to read the map and pull before the answer.
- P runs the server in ``anchors`` mode, pushes nothing, sees the read tools
  without ``activate_context`` and shares M's pull instruction without the map
  line.

The hook command is rendered by the product's own installer, from the build's
own hook sources, so the push is the deployed shape and not a copy of it.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from epistemic.journeys.f27_replay import Arm, custom_instructions_block

from . import protocol
from .tap import BUDGET_REFUSAL, FILTER_REFUSAL

TEXTS = Path(__file__).resolve().parent / "texts"

#: The retrieve hook modes the installer supports (``install_hook.
#: SUPPORTED_ACTIVATION_MODES``); A's is a run input, M's is fixed.
HOOK_MODE_MCP = "mcp"
HOOK_MODE_WORKING_SET = "working-set"

#: A's push. The operator's profile installs bind the retrieve hook with
#: ``--activation-mode mcp``; a run that measures a different deployment says
#: so with ``--a-hook-mode`` and the arm hash records it.
DEFAULT_A_HOOK_MODE = HOOK_MODE_MCP


@dataclass(frozen=True)
class ArmSpec:
    arm_id: str
    packet_mode: str
    #: ``None`` for an arm with no push.
    hook_mode: str | None
    tools: tuple[str, ...]
    #: ``("doc", level)`` for the published prominence block, else text files.
    instruction_source: tuple[str, ...]

    def allowed_tools_flag(self, server_name: str) -> str:
        return " ".join(f"mcp__{server_name}__{tool}" for tool in self.tools)


def arm_specs(*, a_hook_mode: str = DEFAULT_A_HOOK_MODE) -> Mapping[str, ArmSpec]:
    if a_hook_mode not in (HOOK_MODE_MCP, HOOK_MODE_WORKING_SET):
        raise ValueError(f"unsupported hook mode for A: {a_hook_mode!r}")
    p_tools = tuple(tool for tool in protocol.READ_TOOLS if tool != protocol.P_FILTERED_TOOL)
    return {
        "A": ArmSpec("A", "anchors", a_hook_mode, protocol.READ_TOOLS, ("doc", "balanced")),
        "M": ArmSpec(
            "M", "maps", HOOK_MODE_WORKING_SET, protocol.READ_TOOLS, ("pull.md", "map.md")
        ),
        "P": ArmSpec("P", "anchors", None, p_tools, ("pull.md",)),
    }


def client_instructions(spec: ArmSpec) -> tuple[str, str]:
    """``(text, source)``: the arm's client instructions and where they come from.

    Every arm ends with the actor line: the protocol's actor is told not to name
    its tools in the answer.
    """

    if spec.instruction_source[0] == "doc":
        body, source = custom_instructions_block(spec.instruction_source[1])
    else:
        parts = [
            (TEXTS / name).read_text(encoding="utf-8").strip() for name in spec.instruction_source
        ]
        body = "\n\n".join(parts) + "\n"
        source = ", ".join(f"switch_eval/texts/{name}" for name in spec.instruction_source)
    actor = (TEXTS / "actor.md").read_text(encoding="utf-8").strip()
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
    never as an empty hash.
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
        "harness_refusals": sha256_text(BUDGET_REFUSAL + "\n" + FILTER_REFUSAL),
        "packet_mode": spec.packet_mode,
        "server_instructions": (surface or {}).get(
            "server_instructions", "pending: state stage not run"
        ),
        "tool_descriptions": (surface or {}).get(
            "tool_descriptions", "pending: state stage not run"
        ),
    }
    complete = surface is not None
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
