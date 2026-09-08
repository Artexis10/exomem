"""Offline wiring proof with real MCP writes and scripted model decisions."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

from lme import native_pilot as pilot

ROOT = Path(__file__).resolve().parents[1]


class ScriptedMaintenance:
    """Reads real tool replies; no direct access to the memory cell or evaluator."""

    def __init__(self):
        self.requests = []
        self.commits = []

    async def complete_messages(self, messages, *, tools, max_tokens):
        self.requests.append(messages)
        turn = messages[1]["content"]
        replies = [pilot._structured(json.loads(m["content"])) for m in messages if m["role"] == "tool"]
        is_answer = turn.startswith("Question date:")
        correction = "Cedar" in turn
        step = len(replies)
        name, args = None, {}
        if is_answer:
            if step == 0:
                name, args = "ask_memory", {"query": "Lantern launch signal", "mode": "keyword", "prefer_compiled": True}
            elif step == 1:
                hits = replies[-1].get("hits", []) if isinstance(replies[-1], dict) else replies[-1]
                path = next(hit["path"] for hit in hits if "Notes/" in hit["path"] and "updated" in hit["path"])
                name, args = "read_memory", {"path": path, "include_history": True}
            else:
                body = replies[-1]["body"]
                assert "Cedar" in body and "Cobalt" not in body
                message = {"role": "assistant", "content": "Cedar."}
        elif step == 0:
            name, args = "discover_tools", {"names": ["capture_source", "remember", "replace_memory"]}
        elif step == 1:
            name, args = "capture_source", {"title": "Lantern correction" if correction else "Lantern initial conversation", "content": turn, "source_type": "conversation", "response_detail": "full"}
        elif step == 2 and correction:
            name, args = "ask_memory", {"query": "Lantern launch signal", "mode": "keyword", "prefer_compiled": True}
        else:
            source = replies[1]["diagnostics"]
            source = source.get("source", source)
            if step == 2:
                name, args = "remember", {"title": "Lantern launch signal", "note_type": "insight", "content": "# Lantern launch signal\n\n## Claim\n\nThe launch signal is Cobalt.\n", "sources": [source["path"]], "response_detail": "full"}
            elif step == 3 and correction:
                hits = replies[-1].get("hits", []) if isinstance(replies[-1], dict) else replies[-1]
                old = next(hit["path"] for hit in hits if "Notes/" in hit["path"])
                name, args = "replace_memory", {"old_path": old, "title": "Lantern launch signal updated", "note_type": "insight", "content": "# Lantern launch signal updated\n\n## Claim\n\nThe launch signal is Cedar.\n", "reason": "The later conversation explicitly corrected the signal.", "sources": [source["path"]], "response_detail": "full"}
            else:
                assert replies[-1]["status"] == "committed", replies[-1]
                self.commits.append(replies[-1])
                message = {"role": "assistant", "content": "Memory maintained."}
        if name:
            assert name in {t["function"]["name"] for t in tools}
            message = {"role": "assistant", "content": None, "tool_calls": [{"id": f"call-{step}", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]}
        return SimpleNamespace(message=message, input_tokens=100, output_tokens=50, cost_usd=0.0)


def test_native_real_mcp_compilation_correction_and_fresh_recall(tmp_path, monkeypatch):
    question = json.loads((ROOT / "benchmarks/lme/fixtures/leaky.json").read_text())[0]
    question.update(question="What is the Lantern launch signal now?", answer="Cedar", haystack_sessions=[
        [{"role": "user", "content": "For project Lantern, the launch signal is Cobalt."}],
        [{"role": "user", "content": "Correction: for project Lantern, the launch signal is now Cedar."}],
    ])
    dataset = tmp_path / "fixture.json"
    dataset.write_text(json.dumps([question]))
    monkeypatch.setattr(pilot, "_judge_source", lambda _: (b"# offline fixture judge\n", {"commit_sha": "b" * 40}))
    out = tmp_path / "run"
    info = pilot.prepare_native(dataset, Path("fixture-judge"), out, product_root=ROOT, profile="fixture", size=1, budget_cap_usd=1)
    plan = pilot.validate_native_run(out, expected_plan_sha256=info["plan_sha256"])
    execution = out / "execution"
    execution.mkdir()
    backend = ScriptedMaintenance()
    rows, envelope = asyncio.run(pilot.run_native_cases(plan, execution, pilot._snapshots(out, plan), backend=backend, python=Path(sys.executable)))
    row = rows[0]
    assert row["status"] == "completed", (row, backend.requests[-1][-1])
    assert row["hypothesis"] == "Cedar."
    assert envelope.stopped is None
    assert row["before"]["compiled_count"] == 0
    assert row["after_writing"]["compiled_count"] == 2
    assert row["after_answer"]["files"] == row["after_writing"]["files"]
    assert len(backend.commits) == 2
    assert {c["tool"] for p in row["phases"] for c in p["evidence"]["committed_receipts"]} >= {"remember", "replace_memory"}
    answer_inputs = json.loads((execution / "case-0001/answer/input.json").read_text())
    assert "Cobalt" not in json.dumps(answer_inputs)
    assert "Cedar" not in json.dumps(answer_inputs)
    answer_recall = row["phases"][-1]["evidence"]["recall"]
    assert [r["tool"] for r in answer_recall] == ["ask_memory", "read_memory"]
    replacement = backend.commits[-1]["diagnostics"]
    assert replacement["new_ref"] != replacement["old_ref"]
    old = execution / "case-0001/cell/vault" / replacement["old_path"]
    assert "superseded" in old.read_text()
