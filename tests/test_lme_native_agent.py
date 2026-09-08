from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest
from lme.native_agent import AgentLimits, NativeBroker, RunEnvelope, run_agent_phase


class Cell:
    schemas = {
        "ask_memory": {"description": "Recall memories.", "inputSchema": {"type": "object"}},
        "remember": {"description": "Write compiled memory.", "inputSchema": {"type": "object"}},
        "capture_source": {"description": "Capture source text.", "inputSchema": {"type": "object"}},
    }

    def __init__(self):
        self.calls = []

    async def call(self, name, arguments):
        self.calls.append((name, arguments))
        return {"structuredContent": {"result": {"hits": [{"body": "Current choice: Cedar."}]}}}

    def snapshot(self):
        return {"stored_bytes": 0, "files": {}}


class Backend:
    def __init__(self, messages):
        self.messages = iter(messages)
        self.requests = []

    async def complete_messages(self, messages, *, tools, max_tokens):
        self.requests.append({"messages": messages, "tools": tools, "max_tokens": max_tokens})
        return SimpleNamespace(message=next(self.messages), input_tokens=30, output_tokens=10, cost_usd=0.001)


def tool(name, arguments, call_id="call-1"):
    import json
    return {"role": "assistant", "content": None, "tool_calls": [{
        "id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)},
    }]}


def broker(backend, *, limits=None):
    return NativeBroker(
        cell=Cell(), backend=backend, envelope=RunEnvelope(limits or AgentLimits()),
        guidance={"SKILL.md": "Use memory for durable knowledge.", "references/writing.md": "Write through governed tools."},
    )


def test_fresh_answer_worker_obtains_evidence_through_public_tool(tmp_path):
    backend = Backend([tool("ask_memory", {"query": "current choice"}), {"role": "assistant", "content": "Cedar."}])
    b = broker(backend)
    result = asyncio.run(run_agent_phase(b, phase="answer", turn="What is the current choice?", out=tmp_path / "answer"))
    assert result["status"] == "completed"
    assert result["answer"] == "Cedar."
    assert b.cell.calls == [("ask_memory", {"query": "current choice"})]
    assert "Current choice: Cedar." in str(backend.requests[1]["messages"])
    assert result["worker_pid"] != __import__("os").getpid()


@pytest.mark.parametrize("name,args", [
    ("capture_source", {"files": [{"download_url": "https://example.invalid/answers"}]}),
    ("read_file", {"path": "../evaluator.json"}),
    ("discover_tools", {"names": ["transfer_artifact"]}),
])
def test_denied_routes_never_dispatch(name, args):
    b = broker(Backend([]))
    result, _ = asyncio.run(b.tool(name, args, phase="writer"))
    assert result["isError"] is True
    assert b.cell.calls == []


def test_answer_phase_cannot_mutate_memory():
    b = broker(Backend([]))
    result, _ = asyncio.run(b.tool("remember", {"title": "Injected"}, phase="answer"))
    assert result["isError"] is True
    assert b.cell.calls == []


def test_model_budget_survives_fresh_worker_sessions(tmp_path):
    backend = Backend([{"role": "assistant", "content": "No durable update."}])
    b = broker(backend, limits=replace(AgentLimits(), run_model_calls=1))
    first = asyncio.run(run_agent_phase(b, phase="writer", turn="First session.", out=tmp_path / "one"))
    second = asyncio.run(run_agent_phase(b, phase="writer", turn="Second session.", out=tmp_path / "two"))
    assert first["status"] == "completed"
    assert first["tool_calls"] == 0
    assert second["status"] == "incomplete"
    assert "budget" in second["reason"]
    assert len(backend.requests) == 1


def test_worker_context_does_not_carry_previous_phase(tmp_path):
    backend = Backend([{"role": "assistant", "content": "Done."}] * 2)
    b = broker(backend)
    asyncio.run(run_agent_phase(b, phase="writer", turn="HISTORY-SENTINEL", out=tmp_path / "write"))
    asyncio.run(run_agent_phase(b, phase="answer", turn="QUESTION-SENTINEL", out=tmp_path / "read"))
    assert "HISTORY-SENTINEL" not in str(backend.requests[1])
    assert "QUESTION-SENTINEL" not in str(backend.requests[0])


def test_token_budget_settles_usage_and_persists_across_workers(tmp_path):
    backend = Backend([{"role": "assistant", "content": "Done."}])
    b = broker(backend, limits=replace(AgentLimits(), max_context_tokens=2000, run_tokens=6096))
    first = asyncio.run(run_agent_phase(b, phase="writer", turn="One.", out=tmp_path / "one"))
    second = asyncio.run(run_agent_phase(b, phase="writer", turn="Two.", out=tmp_path / "two"))
    assert first["status"] == "completed"
    assert b.envelope.tokens == 40
    assert b.envelope.held_tokens == 0
    assert second["status"] == "incomplete"
    assert "token budget" in second["reason"]
    assert len(backend.requests) == 1


def test_source_special_token_spelling_is_preserved_as_plain_data(tmp_path):
    backend = Backend([{"role": "assistant", "content": "Done."}])
    b = broker(backend)
    result = asyncio.run(run_agent_phase(b, phase="writer", turn="The log says <|endoftext|> literally.", out=tmp_path / "run"))
    assert result["status"] == "completed"
    assert "<|endoftext|>" in backend.requests[0]["messages"][1]["content"]


def test_multi_tool_response_counts_each_operation_against_phase_budget(tmp_path):
    message = tool("ask_memory", {"query": "first"})
    message["tool_calls"].append(tool("ask_memory", {"query": "second"}, "call-2")["tool_calls"][0])
    b = broker(Backend([message]), limits=replace(AgentLimits(), phase_tool_calls=1))
    result = asyncio.run(run_agent_phase(b, phase="answer", turn="Question", out=tmp_path / "run"))
    assert result["status"] == "incomplete"
    assert len(b.cell.calls) == 1
    assert b.envelope.tool_calls == 1


def test_oversized_tool_result_stops_without_silent_truncation(tmp_path):
    b = broker(Backend([tool("ask_memory", {"query": "choice"})]), limits=replace(AgentLimits(), max_tool_result_bytes=10))
    result = asyncio.run(run_agent_phase(b, phase="answer", turn="Question", out=tmp_path / "run"))
    assert result["status"] == "incomplete"
    assert "tool-result byte budget" in result["reason"]


def test_timeout_cancels_pending_model_call_and_reaps_worker(tmp_path):
    class HangingBackend:
        cancelled = False

        async def complete_messages(self, *args, **kwargs):
            try:
                await asyncio.sleep(100)
            except asyncio.CancelledError:
                self.cancelled = True
                raise

    backend = HangingBackend()
    b = broker(backend, limits=replace(AgentLimits(), phase_seconds=0.5))
    result = asyncio.run(run_agent_phase(b, phase="answer", turn="Question", out=tmp_path / "run"))
    assert result["status"] == "incomplete"
    assert result["uncertain_operation"]["kind"] == "model"
    assert backend.cancelled
    with pytest.raises(ProcessLookupError):
        __import__("os").kill(result["worker_pid"], 0)
