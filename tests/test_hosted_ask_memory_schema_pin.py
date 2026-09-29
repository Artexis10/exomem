"""Released Hosted candidates render ask_memory's outputSchema from a pin.

A frozen candidate's compatibility descriptor is a released contract. The live
ask_memory output schema is allowed to move (it was corrected to describe every
shape the tool returns), so the released candidates must not follow it, while
the unreleased ones must.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from exomem import hosted_gateway, hosted_plugins

REPO_ROOT = Path(__file__).resolve().parents[1]
FROZEN = (
    "hosted-alpha-agent-v1",
    "hosted-alpha-agent-v2",
    "hosted-alpha-agent-v3",
    "hosted-alpha-agent-v4",
    "hosted-alpha-agent-v4-command-binding-v1",
)
LIVE = ("hosted-alpha-agent-v5",)


def _rendered(candidate: str) -> bytes:
    return hosted_plugins._canonical_json(
        hosted_plugins.compatibility_manifest(REPO_ROOT, candidate=candidate)
    )


@pytest.fixture()
def moved_live_schema(monkeypatch: pytest.MonkeyPatch) -> None:
    original = hosted_gateway._mcp_tool_contract

    def moved(command, *, descriptor):
        contract = original(command, descriptor=descriptor)
        if command.name == "ask_memory":
            contract = copy.deepcopy(contract)
            contract["outputSchema"]["x-live-schema-moved"] = True
        return contract

    monkeypatch.setattr(hosted_gateway, "_mcp_tool_contract", moved)


def test_moving_the_live_schema_leaves_frozen_candidates_unchanged_and_moves_the_rest(
    moved_live_schema, monkeypatch: pytest.MonkeyPatch
) -> None:
    moved = {c: _rendered(c) for c in (*FROZEN, *LIVE)}
    monkeypatch.undo()
    baseline = {c: _rendered(c) for c in (*FROZEN, *LIVE)}

    for candidate in FROZEN:
        assert moved[candidate] == baseline[candidate], candidate
    for candidate in LIVE:
        assert moved[candidate] != baseline[candidate], candidate


@pytest.mark.parametrize("candidate", FROZEN)
def test_frozen_candidate_renders_the_pinned_released_schema(candidate: str) -> None:
    manifest = hosted_plugins.compatibility_manifest(REPO_ROOT, candidate=candidate)
    (ask,) = [c for c in manifest["agent_contract"]["commands"] if c["name"] == "ask_memory"]
    pin = json.loads(hosted_plugins._ask_memory_pin_path(REPO_ROOT, candidate).read_text("utf-8"))
    assert ask["mcp_tool"]["outputSchema"] == pin
