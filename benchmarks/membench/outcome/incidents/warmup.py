"""Wait for the arm's server to be warm before the actor's first turn.

The incident's client talks to a long-lived service. A server that has just started answers recall with an
``index_warming`` abstention until its retrieval indexes are live, so a session that began inside that window
would grade the product on a cold start that its users never meet. Both actors wait here first. A server that
does not warm within the budget makes the unit unknown (``StillWarming``): the session never ran, so it proves
nothing about the product.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable

#: A neutral turn: it names nothing of the case, so the wait cannot teach the session anything.
WARM_TURN = "Is the knowledge base ready?"
WARM_BUDGET_SECONDS = 120
POLL_SECONDS = 2
#: The reason the product's own activation gives while its recall indexes are cold (``activation_result``); a
#: protocol token that the arm documents, so the harness reads it and never a wording.
WARMING_REASON = "index_warming"


class StillWarming(RuntimeError):
    """The server still answered "warming" when the budget ended."""


def is_warming(answer: object) -> bool:
    return (
        isinstance(answer, dict)
        and (answer.get("abstention") or {}).get("reason") == WARMING_REASON
    )


def answer_of(result: object) -> dict:
    """The ``recall_context`` answer in a tool result, from either MCP client's result type.

    A result that is an error, or whose text is not the server's JSON object, is a fault of the unit and not
    a warm server.
    """
    failed = getattr(result, "is_error", getattr(result, "isError", None))
    try:
        answer = json.loads(result.content[0].text)  # type: ignore[attr-defined]
    except (AttributeError, IndexError, TypeError, ValueError):
        answer = None
    if failed or not isinstance(answer, dict):
        raise RuntimeError(f"recall_context did not answer the warm-up turn: {str(result)[:200]}")
    return answer


async def wait_until_warm(
    ask: Callable[[], Awaitable[dict]],
    *,
    budget: float = WARM_BUDGET_SECONDS,
    poll: float = POLL_SECONDS,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> float:
    """Ask until the answer is not a warming abstention. Returns the seconds waited."""
    started = clock()
    while is_warming(await ask()):
        if clock() - started >= budget:
            raise StillWarming(f"the server still answered {WARMING_REASON} after {budget:g} s")
        await sleep(poll)
    return round(clock() - started, 1)


async def await_warm(ask: Callable[[], Awaitable[dict]], record: dict, **bounds) -> None:
    """``wait_until_warm``, leaving its seconds in the unit's record; None while it has not succeeded."""
    record["warm_seconds"] = None
    record["warm_seconds"] = await wait_until_warm(ask, **bounds)


def ask_over(config: dict, turn: str = WARM_TURN) -> Callable[[], Awaitable[dict]]:
    """An ``ask`` that calls ``recall_context`` with ``turn`` on the server of an ``--mcp-config`` entry (a loopback server).

    The client is the one the product ships with. It connects for each question, so a server that is not
    answering yet is one more warming answer's worth of waiting and not a crash of the wait.
    """
    from fastmcp import Client

    async def ask() -> dict:
        async with Client({"mcpServers": {"exomem": config}}) as client:
            return answer_of(
                await client.call_tool("recall_context", {"turn": turn}, raise_on_error=False)
            )

    return ask
