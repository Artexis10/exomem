"""The OpenAI actor: an Agents SDK agent on the arm's own stdio Exomem server, with no hooks.

It is the client class of a chat client that has MCP tools and the skill text but no hook surface, which is
where an incident can happen that the Claude Code plugin's hooks would have prevented. The agent's instructions
are the arm's Exomem skill files (the replay suite's, from the arm's scaffold) followed by the arm's server
instructions, and nothing is injected before the turn. The server is the arm's own (``isolation.mcp_server``) on
the unit's own vault, so a tool call reaches nothing else.

The SDK is not a dependency of the product. Run the harness under ``uv run --no-project --python <interpreter
that has the product's dependencies> --with openai-agents`` so that it layers over that interpreter, and under an
``OPENAI_API_KEY`` for the eval account. A provider error, a timeout or a server fault is a fault of the unit
(unknown), never a failed unit.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import subprocess
import time
from collections.abc import Mapping
from pathlib import Path

from ..isolation import mcp_server
from . import warmup
from . import withhold as withholding
from .turns import user_turns

#: The default actor model; ``--model`` overrides it.
OPENAI_MODEL = "gpt-6.1-sol"
#: Model responses a session may take. Finding the declared method, reading the collection and filing a row
#: take about a dozen tool calls, and a session that does not conclude in this many is a fault of the unit.
MAX_TURNS = 30
#: The name that the spend of these runs is metered under (``harness openai-credits record``).
CREDITS_CONSUMER = "capture-routing-eval"
SERVER_CALL_TIMEOUT_SECONDS = 180
MODEL_TIMEOUT_SECONDS = 180
MODEL_RETRIES = 2
CREDITS_TIMEOUT_SECONDS = 60


def require_sdk(environ: dict[str, str]) -> None:
    """Refuse a run that cannot play the actor, before any run directory or vault exists."""
    if importlib.util.find_spec("agents") is None:
        raise SystemExit(
            "--actor openai needs the OpenAI Agents SDK: run under `uv run --no-project --python <interpreter "
            "with the product's dependencies> --with openai-agents python -m membench.outcome.incidents ...`"
        )
    if not environ.get("OPENAI_API_KEY"):
        raise SystemExit(
            "--actor openai needs OPENAI_API_KEY (the eval account's key); none is set"
        )


def arguments_of(raw: object) -> object:
    """A tool call's arguments as the object the model sent; the text itself when it is not JSON."""
    try:
        return json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return raw


def output_text(output: object) -> str:
    """A tool's output as the text the server sent: the SDK hands an MCP text block, or a list of them, back."""
    blocks = output if isinstance(output, list) else [output]
    if all(isinstance(block, dict) and "text" in block for block in blocks):
        return "\n".join(block["text"] for block in blocks)
    return str(output)


def tool_calls(items: list) -> list[dict]:
    """Every tool call of the run in order, paired by call id with the result that came back.

    The shape is the Claude actor's: ``name``, ``input``, ``result`` (None when none came back) and ``is_error``
    (None, because the SDK hands a tool's failure back as its result, which is the server's own error envelope).
    """
    calls: list[dict] = []
    for item in items:
        raw = getattr(item, "raw_item", None)
        if getattr(item, "type", "") == "tool_call_item" and raw is not None:
            calls.append(
                {
                    "name": getattr(raw, "name", None),
                    "input": arguments_of(getattr(raw, "arguments", None)),
                    "call_id": getattr(raw, "call_id", None),
                    "result": None,
                    "is_error": None,
                }
            )
        elif getattr(item, "type", "") == "tool_call_output_item" and raw is not None:
            call_id = raw.get("call_id") if isinstance(raw, dict) else getattr(raw, "call_id", None)
            # Parallel calls return their outputs out of order, so each output finds its call by id.
            owner = next((call for call in calls if call["call_id"] == call_id), None)
            if owner is not None:
                owner["result"] = output_text(getattr(item, "output", ""))
    return [{key: value for key, value in call.items() if key != "call_id"} for call in calls]


def usage_of(responses: list) -> dict[str, int]:
    """The run's token usage, summed over its model responses."""
    usage = {"input": 0, "cached": 0, "output": 0}
    for response in responses:
        part = response.usage
        usage["input"] += part.input_tokens
        usage["output"] += part.output_tokens
        usage["cached"] += (
            getattr(getattr(part, "input_tokens_details", None), "cached_tokens", 0) or 0
        )
    return usage


def record_spend(model: str, usage: dict[str, int]) -> str:
    """Meter the run's tokens through the harness, which prices them from its catalog; its receipt line.

    A spend that could not be recorded is said so in the unit's record, and does not change the unit's result.
    """
    argv = [
        "harness",
        "openai-credits",
        "record",
        "--consumer",
        CREDITS_CONSUMER,
        "--model",
        model,
        "--input-tokens",
        str(usage["input"]),
        "--cached-input-tokens",
        str(usage["cached"]),
        "--output-tokens",
        str(usage["output"]),
    ]
    try:
        done = subprocess.run(  # noqa: S603 - fixed argv
            argv, capture_output=True, text=True, timeout=CREDITS_TIMEOUT_SECONDS
        )
    except (OSError, subprocess.SubprocessError) as error:
        return f"not recorded: {error!r:.200}"
    text = (done.stdout or done.stderr).strip()[-300:]
    return text if done.returncode == 0 else f"not recorded (exit {done.returncode}): {text}"


async def run_turns(run, turns: list[str], trace: dict) -> list[str]:
    """Each user turn in order on one agent, the conversation so far carried into the next; each turn's answer.

    ``run`` plays one input and returns the SDK's run result: the first turn's text, then the conversation so
    far (``to_input_list``, the SDK's own way to continue a conversation) with the next user turn after it.
    What a turn spent and did joins ``trace`` as soon as it ends, so a later turn that fails leaves it.
    """
    answers: list[str] = []
    conversation: object = turns[0]
    for later in [*turns[1:], None]:
        result = await run(conversation)
        trace["responses"] += result.raw_responses
        trace["items"] += result.new_items
        answers.append(str(result.final_output or "").strip())
        if later is not None:
            conversation = result.to_input_list() + [{"role": "user", "content": later}]
    return answers


async def converse(
    spec: dict,
    root: Path,
    instructions: str,
    turns: list[str],
    model: str,
    max_turns: int,
    record: dict,
    trace: dict,
) -> list[str]:
    """The user turns, as one session on one agent and one server, once that server is warm (``warmup``); each answer.

    The server is this actor's own stdio process, so the wait runs on the connection that the session then uses.
    ``trace`` collects the model responses and tool-call items of every turn (``run_turns``).
    """
    from agents import Agent, ModelSettings, OpenAIResponsesModel, Runner, set_tracing_disabled
    from agents.mcp import MCPServerStdio
    from openai import AsyncOpenAI

    # The run's prompts and tool results stay with the run; nothing is exported to the provider's trace store.
    set_tracing_disabled(True)
    # A client of this run's own event loop: the SDK's shared default client is bound to the first loop, so a
    # second unit in the same process would fail on it. The timeout and the retry count are the run's bound.
    client = AsyncOpenAI(timeout=MODEL_TIMEOUT_SECONDS, max_retries=MODEL_RETRIES)
    try:
        async with MCPServerStdio(
            name="exomem",
            params={
                "command": spec["command"],
                "args": spec["args"],
                "env": spec["env"],
                # The unit root holds no `.env`; the caller's directory may, and the server loads it over `env`.
                "cwd": str(root),
            },
            cache_tools_list=True,
            client_session_timeout_seconds=SERVER_CALL_TIMEOUT_SECONDS,
        ) as server:

            async def ask() -> dict:
                return warmup.answer_of(
                    await server.call_tool("recall_context", {"turn": warmup.WARM_TURN})
                )

            await warmup.await_warm(ask, record)
            agent = Agent(
                name="assistant",
                model=OpenAIResponsesModel(model=model, openai_client=client),
                mcp_servers=[server],
                instructions=instructions,
                model_settings=ModelSettings(include_usage=True),
            )
            return await run_turns(
                lambda conversation: Runner.run(agent, conversation, max_turns=max_turns),
                turns,
                trace,
            )
    finally:
        await client.close()


def keep_run(
    record: dict, root: Path, model: str, traces: list[dict], started: float
) -> list[list[dict]]:
    """What a run leaves whether it finished or stopped part-way: its tokens metered over every session, its tool
    calls written down. Returns each session's calls, the main session's first.

    ``traces`` holds one ``{"responses", "items"}`` per session (``run_turns``): the main session, then the
    follow-up's when the case has one.
    """
    usage = usage_of([response for trace in traces for response in trace["responses"]])
    sessions = [tool_calls(trace["items"]) for trace in traces]
    record |= {
        "seconds": round(time.monotonic() - started, 1),
        "model_reported": model,
        "usage": usage,
        "spend_receipt": record_spend(model, usage),
    }
    (root / "calls.json").write_text(json.dumps(sessions[0], indent=1, default=str))
    if len(sessions) > 1:
        (root / "calls-follow-up.json").write_text(json.dumps(sessions[1], indent=1, default=str))
    return sessions


def play(
    case: dict,
    arm: Path,
    root: Path,
    record: dict,
    *,
    server_instructions: str,
    model: str,
    max_turns: int,
    timeout: float,
    arm_env: Mapping[str, str] | None = None,
    withhold: dict | None = None,
) -> dict:
    """Run the case's session on the seeded vault; returns it as ``__main__.play_claude`` documents.

    The turns are one session. A ``follow_up`` is a second session on the same vault: a fresh server, agent
    and conversation, as a later day's client would have. With ``withhold`` (``arm_side.py withhold``) the main
    session's server runs behind the relay that withholds one write's acknowledgement (``withhold.py``); the
    follow-up's does not.

    The skill files are read at run time from the arm that is run, and ``server_instructions`` come from its
    own server, so neither can drift from what the arm's users have.
    """
    # The repository's own skill reader, so this actor and the import-guidance suites show the same text.
    from import_guidance.runner import skill_text
    from import_guidance.scripted_replies import REPLAY_SUITE

    instructions = (
        f"{skill_text(arm, REPLAY_SUITE)}\n\n# Exomem server instructions\n\n{server_instructions}"
    )
    record["instructions_chars"] = len(instructions)
    # This actor always runs its own stdio server, whatever transport the case names for a client that cannot.
    record["transport"] = "stdio"
    sessions = [user_turns(case)] + ([[case["follow_up"]]] if "follow_up" in case else [])
    traces: list[dict] = []
    answers: list[list[str]] = []
    started = time.monotonic()
    try:
        for number, session_turns in enumerate(sessions):
            traces.append({"responses": [], "items": []})
            # The follow-up's own wait would overwrite the main session's `warm_seconds`.
            warm: dict = record if number == 0 else {}
            spec = mcp_server(root, arm, arm_env)
            answers.append(
                asyncio.run(
                    asyncio.wait_for(
                        converse(
                            withholding.relayed(spec, withhold, root)
                            if withhold and number == 0
                            else spec,
                            root,
                            instructions,
                            session_turns,
                            model,
                            max_turns,
                            warm,
                            traces[number],
                        ),
                        timeout * len(session_turns),
                    )
                )
            )
            if number:
                record["follow_up_warm_seconds"] = warm.get("warm_seconds")
    except Exception as error:  # noqa: BLE001 - a provider error, a timeout or a server fault is unknown, never a product failure
        # The SDK attaches what a run that stopped part-way (the turn limit, a bad model output) had done by then,
        # and its tokens were spent; any other fault leaves only the turns that had already finished.
        stopped = getattr(error, "run_data", None)
        if stopped is not None:
            traces[-1]["responses"] += stopped.raw_responses
            traces[-1]["items"] += stopped.new_items
        if not any(trace["responses"] or trace["items"] for trace in traces):
            record["seconds"] = round(time.monotonic() - started, 1)
            record["usage"] = "unknown: the run did not return its responses"
            calls: list[dict] = []
        else:
            calls = keep_run(record, root, model, traces, started)[0]
            if stopped is None:
                record["usage_partial"] = "the turn that failed returned no responses"
        return {
            "answer": "",
            "tools": [call["name"] for call in calls],
            "calls": calls,
            "fault": f"{type(error).__name__}: {str(error)[:300]}",
        }
    calls_by_session = keep_run(record, root, model, traces, started)
    main = answers[0]
    (root / "answer.txt").write_text(main[-1])
    if len(sessions) > 1 or len(main) > 1:
        (root / "answers.json").write_text(
            json.dumps(
                {"turns": main, "follow_up": answers[1][0] if len(answers) > 1 else None}, indent=1
            )
        )
    follow_up = (
        {
            "answer": answers[1][0],
            "tools": [call["name"] for call in calls_by_session[1]],
            "calls": calls_by_session[1],
        }
        if len(answers) > 1
        else None
    )
    return {
        "answer": main[-1],
        "answers": main,
        "tools": [call["name"] for call in calls_by_session[0]],
        "calls": calls_by_session[0],
        "follow_up": follow_up,
        "fault": None,
    }
