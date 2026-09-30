"""UtilityBroker: same actor/limits for both arms, arm-scoped tool policy."""
from __future__ import annotations

import asyncio
import json

from lme.native_agent import AgentLimits, RunEnvelope
from membench.utility.action_world import ActionWorld
from membench.utility.actor import UtilityBroker, phase_messages, run_phase
from membench.utility.scenarios import actor_view, generate_episode

GUIDANCE = {"SKILL.md": "SKILL-SENTINEL", "references/writing.md": "WRITING-SENTINEL"}


def test_renderer_removes_only_proven_duplicate_text():
    from membench.utility.actor import render_mcp_result
    value = {"answer": "durable result"}
    original = {"structuredContent": value, "content": [{"type": "text", "text": json.dumps(value)}], "isError": False}
    rendered = render_mcp_result(original)
    assert rendered == {"structuredContent": value, "isError": False}
    assert "content" in original
    wrapped = {**original, "structuredContent": {"result": value}}
    assert render_mcp_result(wrapped) == {"structuredContent": {"result": value}, "isError": False}
    for block in ({"type": "text", "text": "distinct evidence"},
                  {"type": "text", "text": json.dumps(value), "annotations": {"priority": 1}}):
        envelope = {**original, "content": [block]}
        assert render_mcp_result(envelope) == envelope


class FakeCell:
    schemas = {
        "bootstrap": {"description": "Bootstrap.", "inputSchema": {"type": "object"}},
        "ask_memory": {"description": "Recall memories.", "inputSchema": {"type": "object"}},
        "remember": {"description": "Write compiled memory.", "inputSchema": {"type": "object"}},
    }

    def __init__(self):
        self.calls = []

    async def call(self, name, arguments):
        self.calls.append((name, arguments))
        return {"structuredContent": {"result": "ok"}}

    def snapshot(self):
        return {"stored_bytes": 0, "files": {}}


class FakeBackend:
    def __init__(self, messages):
        self.messages = iter(messages)
        self.requests = []

    async def complete_messages(self, messages, *, tools, max_tokens):
        self.requests.append({"messages": messages, "tools": tools, "max_tokens": max_tokens})
        from types import SimpleNamespace
        return SimpleNamespace(message=next(self.messages), input_tokens=10, output_tokens=5, cost_usd=0.001)


def _tool_call(name, arguments, call_id="call-1"):
    return {"role": "assistant", "content": None, "tool_calls": [{
        "id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)},
    }]}


def _broker(arm, cell=None, backend=None):
    episode = generate_episode(seed=1, variant="helpful_history")
    world = ActionWorld(episode, arm=arm)
    return UtilityBroker(
        world=world, arm=arm, cell=cell or FakeCell(), backend=backend or FakeBackend([]),
        envelope=RunEnvelope(AgentLimits()), guidance=GUIDANCE,
    )


def test_control_arm_has_no_memory_tools_or_discovery():
    broker = _broker("control")
    available = broker.available("action")
    assert "bootstrap" not in available
    assert "ask_memory" not in available
    assert "discover_tools" not in available
    assert "read_file" not in available
    assert "list_projects" in available
    assert "apply_config" in available


def test_memory_arm_has_world_and_shipped_memory_tools():
    broker = _broker("memory")
    available = broker.available("experience")
    assert "bootstrap" in available
    assert "ask_memory" in available
    assert "remember" in available
    assert "discover_tools" in available
    assert "read_file" in available
    assert "apply_config" in available


def test_same_tool_policy_applies_to_all_three_phases():
    broker = _broker("memory")
    assert broker.available("experience") == broker.available("change") == broker.available("action")


def test_messages_carry_no_seed_variant_or_oracle():
    episode = generate_episode(seed=7, variant="stale_distractor")
    view = actor_view(episode, 0)
    broker = _broker("memory")
    messages = phase_messages(broker, view)
    blob = json.dumps(messages)
    assert str(episode.seed) not in blob
    assert episode.variant not in blob
    assert episode.oracle.target_project not in blob or episode.oracle.target_project in view.narrative
    assert "oracle" not in blob.lower()


def test_control_messages_have_no_shipped_guidance():
    episode = generate_episode(seed=1, variant="helpful_history")
    view = actor_view(episode, 0)
    broker = _broker("control")
    messages = phase_messages(broker, view)
    blob = json.dumps(messages)
    assert "SKILL-SENTINEL" not in blob
    assert view.narrative in blob


def test_memory_messages_include_shipped_guidance():
    episode = generate_episode(seed=1, variant="helpful_history")
    view = actor_view(episode, 0)
    broker = _broker("memory")
    messages = phase_messages(broker, view)
    blob = json.dumps(messages)
    assert "SKILL-SENTINEL" in blob


def test_run_phase_dispatches_world_tool_calls_to_the_action_world(tmp_path):
    episode = generate_episode(seed=1, variant="self_contained")
    view = actor_view(episode, 2)
    args = {"project": episode.oracle.target_project, "steps": list(episode.oracle.current_state.steps),
            "constraint": episode.oracle.current_state.constraint}
    backend = FakeBackend([_tool_call("apply_config", args), {"role": "assistant", "content": "Done."}])
    world = ActionWorld(episode, arm="control")
    world.advance_phase(2)
    broker = UtilityBroker(world=world, arm="control", cell=FakeCell(), backend=backend,
                            envelope=RunEnvelope(AgentLimits()), guidance=GUIDANCE)
    result = asyncio.run(run_phase(broker, view=view, out=tmp_path / "run"))
    assert result["status"] == "completed"
    outcome = world.grade()
    assert outcome.success is True


def test_run_phase_memory_tool_calls_reach_the_cell(tmp_path):
    episode = generate_episode(seed=1, variant="helpful_history")
    view = actor_view(episode, 0)
    backend = FakeBackend([_tool_call("ask_memory", {"query": "steps"}), {"role": "assistant", "content": "Noted."}])
    cell = FakeCell()
    world = ActionWorld(episode, arm="memory")
    broker = UtilityBroker(world=world, arm="memory", cell=cell, backend=backend,
                            envelope=RunEnvelope(AgentLimits()), guidance=GUIDANCE)
    result = asyncio.run(run_phase(broker, view=view, out=tmp_path / "run"))
    assert result["status"] == "completed"
    assert cell.calls == [("ask_memory", {"query": "steps"})]
