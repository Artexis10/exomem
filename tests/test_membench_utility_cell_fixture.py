"""One bounded end-to-end pass over the real product cell, no paid model call.

Explicitly synthetic: the model is a scripted fake and the cell runs the
lexical `fixture` profile, so nothing here is evidence of live utility. What
it does establish is that the shipped memory tools, the loaded scaffold
guidance and the action world actually work together through the real worker.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from lme.native_agent import AgentLimits, RunEnvelope
from lme.native_cell import NativeCell

from membench.utility.action_world import ActionWorld
from membench.utility.actor import UtilityBroker, run_phase
from membench.utility.runner import _load_guidance
from membench.utility.scenarios import actor_view, generate_episode

ROOT = Path(__file__).resolve().parents[1]


class ScriptedModel:
    """A fake model: one scripted reply per turn, no network, no credential."""

    def __init__(self, replies):
        self.replies = iter(replies)
        self.seen = []

    async def complete_messages(self, messages, *, tools, max_tokens):
        self.seen.append({"messages": messages, "tools": [t["function"]["name"] for t in tools]})
        return SimpleNamespace(message=next(self.replies), input_tokens=50, output_tokens=20, cost_usd=0.0)


def _call(name, arguments, call_id):
    return {"role": "assistant", "content": None, "tool_calls": [{
        "id": call_id, "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)}}]}


def test_memory_arm_uses_real_shipped_tools_and_guidance(tmp_path):
    episode = generate_episode(seed=5, variant="helpful_history")
    guidance = _load_guidance(ROOT)
    assert "SKILL.md" in guidance and len(guidance) > 1

    backend = ScriptedModel([
        _call("bootstrap", {"profile": "compact"}, "call-1"),
        _call("discover_tools", {"names": ["remember"]}, "call-discover"),
        _call("remember", {"content": "## Observations\n\n- [operating constraint] Project configuration requires bounded retries #reliability\n",
                           "title": "Project configuration", "note_type": "insight"}, "call-2"),
        _call("ask_memory", {"query": "Project configuration bounded retries"}, "call-3"),
        {"role": "assistant", "content": "Captured."},
    ])

    async def exercise():
        async with NativeCell(tmp_path / "cell", python=Path(sys.executable),
                              product_root=ROOT, profile="fixture") as cell:
            world = ActionWorld(episode, arm="memory")
            before = cell.snapshot()
            broker = UtilityBroker(world=world, arm="memory", cell=cell, backend=backend,
                                   envelope=RunEnvelope(AgentLimits(phase_model_calls=6, phase_seconds=45,
                                                                    run_seconds=50)),
                                   guidance=guidance)
            result = await run_phase(broker, view=actor_view(episode, 0), out=tmp_path / "phase")
            readiness = await cell.readiness()
            return result, cell.snapshot(), readiness, before

    result, snapshot, readiness, before = asyncio.run(exercise())
    assert result["status"] == "completed", result.get("reason")
    assert result["tool_calls"] == 4
    assert snapshot["compiled_count"] > before["compiled_count"], "remember must create a compiled note"
    events = [json.loads(line) for line in (tmp_path / "phase" / "events.jsonl").read_text().splitlines()]
    responses = [e["result"] for e in events if e.get("direction") == "response" and "result" in e]
    assert all(not r.get("isError") for r in responses), responses
    assert "Project configuration" in json.dumps(responses[-1]), "actual recall must return the captured note"
    for event in events:
        if "raw_result" in event:
            raw, delivered = event["raw_result"], event["result"]
            assert raw["structuredContent"] == delivered["structuredContent"]
            assert "content" in raw and "content" not in delivered
    assert readiness["status"] in {"ready", "ready_lexical_only"}
    # The fixture profile is lexical and never the shipped semantic default.
    assert readiness["semantic_requested"] is False
    assert readiness["status"] == "ready_lexical_only"


def test_guidance_loads_every_documented_reference_path():
    guidance = _load_guidance(ROOT)
    schema_dir = ROOT / "src" / "exomem" / "_scaffold" / "_Schema"
    documented = {f"references/{path.name}" for path in (schema_dir / "references").glob("*.md")}
    assert documented <= set(guidance)
    for name in ("references/recall.md", "references/writing.md", "references/operations.md"):
        assert guidance.get(name)


def test_guidance_refuses_a_documented_path_that_is_missing(tmp_path):
    from membench.utility.runner import UtilityRunnerError

    schema = tmp_path / "src" / "exomem" / "_scaffold" / "_Schema"
    (schema / "references").mkdir(parents=True)
    (schema / "SKILL.md").write_text("See [recall](references/recall.md).", encoding="utf-8")
    with pytest.raises(UtilityRunnerError, match="recall"):
        _load_guidance(tmp_path)
