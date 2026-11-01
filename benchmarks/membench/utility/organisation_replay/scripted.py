"""A model-free scripted MCP session through the switch-evaluation tap.

Each :class:`ScriptedSession` is its own client connection to its own server
process, so it is a separate MCP session from every other one. Every call goes
through the tap, which logs it beside the measured sessions' calls.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path

from membench.utility.switch_eval.arms import ArmSpec
from membench.utility.switch_eval.state import Slot, mcp_server_entry
from membench.utility.switch_eval.tap import ALLOW_ALL, READY_TIMEOUT_SECONDS

from .runfile import refusal, structured_answer

#: Scripted sessions are setup, not the actor's turns: the per-turn guard is
#: the measured sessions' rule, so a seed of any size passes the tap here.
SCRIPTED_BUDGET = 100_000


class DoorFault(RuntimeError):
    """A public door refused or could not be reached; the seed is a harness fault."""


@dataclass(frozen=True)
class Doors:
    """Product commands spelled as the tools this arm's discovery serves."""

    served: frozenset[str]

    def spell(self, command: str, arguments: Mapping) -> tuple[str, dict]:
        # The arm's own package spells the call, so each arm uses its own
        # tool and operation names; discovery then has to serve that tool.
        from exomem.commands import product_commands_for
        from exomem.operation_entries import spell_call

        tool, spelled = spell_call(command, dict(arguments), "nested")
        if tool == command:
            public = {c.name: c.public_name for c in product_commands_for("mcp")}
            tool = public.get(command, command)
        if tool not in self.served:
            raise DoorFault(f"{command} spells as {tool!r}, which discovery does not serve")
        return tool, spelled


def payload(result) -> object:
    """The structured answer of one call, or its text parsed as JSON."""

    if result.structured_content is not None:
        return structured_answer(result.structured_content, result.meta)
    texts = [getattr(item, "text", "") for item in result.content]
    try:
        return json.loads("".join(texts))
    except json.JSONDecodeError:
        return {"text": "".join(texts)}


def retry_after(answer: object) -> float | None:
    """Seconds to wait before resending a refused call, or ``None``.

    The product marks a refusal that committed nothing and clears on its own
    (warm-up, a busy boundary) with ``status: retryable`` and ``retry_after_ms``;
    its contract is to resend the same call. Any other refusal is final.
    """

    error = answer.get("error") if isinstance(answer, dict) else None
    if not isinstance(error, dict) or refusal(answer) is None:
        return None
    wait = error.get("retry_after_ms")
    if error.get("status") != "retryable" or error.get("committed") is not False:
        return None
    if not isinstance(wait, int) or isinstance(wait, bool):
        return None
    return wait / 1000


class ScriptedSession:
    """One scripted client connection: ``async with ScriptedSession(...) as s``."""

    def __init__(
        self,
        slot: Slot,
        spec: ArmSpec,
        *,
        env: Mapping[str, str],
        log_dir: Path,
        name: str,
        doors: Doors | None = None,
        ready_gate: bool = False,
    ) -> None:
        log_dir.mkdir(parents=True, exist_ok=True)
        self.tap_log = log_dir / f"{name}-tap.jsonl"
        entry = mcp_server_entry(
            slot,
            replace(spec, tools=(ALLOW_ALL,)),
            log=self.tap_log,
            server_stderr=log_dir / f"{name}-server.stderr",
            skip_ready_gate=not ready_gate,
            warm_search=ready_gate,
            budget=SCRIPTED_BUDGET,
        )
        self._transport_args = (entry["command"], entry["args"], {**env, **entry["env"]}, slot.cwd)
        self._slot = slot
        self.doors = doors
        self.calls: list[dict] = []
        #: Each refused call that the session resent, for the seed build facts.
        self.retries: list[dict] = []
        self._client = None

    async def __aenter__(self) -> ScriptedSession:
        from fastmcp import Client
        from fastmcp.client.transports import StdioTransport

        command, args, env, cwd = self._transport_args
        self._slot.turn_file.write_text("0", encoding="utf-8")
        transport = StdioTransport(command=command, args=list(args), env=dict(env), cwd=str(cwd))
        self._client = Client(transport)
        await self._client.__aenter__()
        if self.doors is None:
            self.doors = Doors(frozenset(tool.name for tool in await self._client.list_tools()))
        return self

    async def __aexit__(self, *exc) -> None:
        await self._client.__aexit__(*exc)

    async def call(self, command: str, arguments: Mapping) -> dict:
        """One call through its door; a refused or failed call is a :class:`DoorFault`."""

        answer = await self.call_raw(command, arguments)
        code = refusal(answer)
        if code is not None:
            raise DoorFault(
                f"{command} {arguments.get('operation', '')}: {code}: {json.dumps(answer)[:600]}"
            )
        return answer

    async def call_raw(self, command: str, arguments: Mapping) -> object:
        tool, spelled = self.doors.spell(command, arguments)
        # A server still warming refuses writes as retryable; the measured
        # sessions' ready gate waits as long, so the seed waits by the same bound.
        deadline = time.monotonic() + READY_TIMEOUT_SECONDS
        while True:
            result = await self._client.call_tool(tool, spelled, raise_on_error=False)
            answer = payload(result)
            self.calls.append({"command": command, "tool": tool, "is_error": result.is_error})
            if result.is_error:
                raise DoorFault(f"{tool}: MCP error: {json.dumps(answer)[:600]}")
            wait = retry_after(answer)
            if wait is None or time.monotonic() + wait > deadline:
                return answer
            self.retries.append(
                {"command": command, "code": refusal(answer), "retry_after_ms": int(wait * 1000)}
            )
            await asyncio.sleep(wait)
