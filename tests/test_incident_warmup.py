"""A unit must not measure a cold-start window by accident: the actor's first turn waits for a warm server.

The incident's client talks to a long-lived service. A server that has just started answers the first recall
with an `index_warming` abstention, so a session that began inside that window would grade the product on
retrieval that its real users never see.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from membench.outcome.incidents import __main__ as incidents
from membench.outcome.incidents import warmup
from membench.outcome.incidents.server import loopback_mcp
from membench.outcome.isolation import REPO_ROOT

WARMING = {"abstained": True, "abstention": {"reason": "index_warming"}}
READY = {"abstained": False, "task_context": {"state": "empty"}}


class Clock:
    """A fake clock that its own sleep advances, so a bounded wait runs without waiting."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds


def wait(answers: list[dict], *, budget: float = 30, poll: float = 2) -> tuple[float | None, list]:
    """Run the wait against a server that gives these answers in turn, the last one for ever."""
    clock, asked = Clock(), []

    async def ask() -> dict:
        asked.append(clock.now)
        return answers[min(len(asked), len(answers)) - 1]

    waited = asyncio.run(
        warmup.wait_until_warm(ask, budget=budget, poll=poll, clock=clock, sleep=clock.sleep)
    )
    return waited, asked


def test_the_wait_ends_on_the_first_answer_that_is_not_a_warming_abstention() -> None:
    waited, asked = wait([WARMING, WARMING, READY])
    assert waited == 4.0 and asked == [0.0, 2.0, 4.0]


def test_a_server_that_is_already_warm_costs_no_wait() -> None:
    assert wait([READY]) == (0.0, [0.0])


def test_an_abstention_for_another_reason_is_not_a_warming_server() -> None:
    """Waiting out a refusal that will not clear would hold the unit for the whole budget."""
    other = {"abstained": True, "abstention": {"reason": "disabled"}}
    assert wait([other])[0] == 0.0


def test_a_server_that_never_warms_is_a_timeout_and_not_an_answer_the_session_could_use() -> None:
    clock = Clock()

    async def ask() -> dict:
        return WARMING

    with pytest.raises(warmup.StillWarming, match="30"):
        asyncio.run(warmup.wait_until_warm(ask, budget=30, poll=2, clock=clock, sleep=clock.sleep))
    assert clock.now >= 30


def test_the_answer_is_read_from_the_servers_json_text_whichever_client_made_the_call() -> None:
    text = SimpleNamespace(type="text", text=json.dumps(WARMING))
    assert warmup.answer_of(SimpleNamespace(content=[text], is_error=False)) == WARMING
    assert warmup.answer_of(SimpleNamespace(content=[text], isError=False)) == WARMING


def test_an_error_result_or_unreadable_text_is_a_fault_and_not_a_warm_server() -> None:
    text = SimpleNamespace(type="text", text="boom")
    with pytest.raises(RuntimeError, match="recall_context"):
        warmup.answer_of(SimpleNamespace(content=[text], is_error=True))
    with pytest.raises(RuntimeError, match="recall_context"):
        warmup.answer_of(SimpleNamespace(content=[text], is_error=False))


def test_the_unit_record_keeps_the_seconds_waited_and_says_none_when_the_wait_ran_out() -> None:
    """A timeout is unknown, never a seconds figure that looks like a warm start."""
    clock, record = Clock(), {}

    async def warm_after_two() -> dict:
        return READY if clock.now >= 2 else WARMING

    asyncio.run(
        warmup.await_warm(warm_after_two, record, budget=30, poll=2, clock=clock, sleep=clock.sleep)
    )
    assert record["warm_seconds"] == 2.0

    async def never() -> dict:
        return WARMING

    record, clock = {}, Clock()
    with pytest.raises(warmup.StillWarming):
        asyncio.run(
            warmup.await_warm(never, record, budget=4, poll=2, clock=clock, sleep=clock.sleep)
        )
    assert record["warm_seconds"] is None


def served_methods(root, *, remote_owner: bool, turn: str) -> tuple[str, list]:
    """What the loopback server gives a client's turn after the harness's own wait: the task context's state and methods."""

    def mint(session: dict) -> str:
        return incidents.run_arm_side(REPO_ROOT, root, "mint", stdin=json.dumps(session))["token"]

    with loopback_mcp(root, REPO_ROOT, mint, remote_owner=remote_owner) as server:
        asyncio.run(warmup.await_warm(warmup.ask_over(server), {}))
        task = asyncio.run(warmup.ask_over(server, turn)())["task_context"]
    return task["state"], task["methods"]["items"]


@pytest.mark.timeout(300)
def test_the_loopback_server_serves_a_declared_method_only_to_a_sign_in_that_acts_as_the_owner(
    tmp_path,
) -> None:
    """The Claude actor reads what this server serves. A sign-in that is a separate principal gets no task
    context at all, so a unit on it would grade the product on a method that no owner is ever denied."""
    case = incidents.load_cases(["existing-workflow"])[0]
    _seeded, seed, _work = incidents.seed_unit(case, REPO_ROOT, tmp_path)
    assert case["remote_owner"] is True
    state, items = served_methods(tmp_path, remote_owner=True, turn=case["prompt"])
    # The owner can read the declared method while startup still adopts the graph for task links.
    assert state in {"ready", "partial"}
    assert [item["source_ref"] for item in items] == [seed["collections"][0]["method_page"]]
    assert served_methods(tmp_path, remote_owner=False, turn=case["prompt"]) == ("unavailable", [])
